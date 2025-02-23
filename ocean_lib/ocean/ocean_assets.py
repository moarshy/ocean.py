#
# Copyright 2022 Ocean Protocol Foundation
# SPDX-License-Identifier: Apache-2.0
#

"""Ocean module."""
import json
import logging
import lzma
import os
from typing import List, Optional, Tuple, Type, Union

from enforce_typing import enforce_types
from web3 import Web3

from ocean_lib.agreements.consumable import AssetNotConsumable, ConsumableCodes
from ocean_lib.agreements.service_types import ServiceTypes
from ocean_lib.aquarius import Aquarius
from ocean_lib.assets.asset import Asset
from ocean_lib.assets.asset_downloader import download_asset_files, is_consumable
from ocean_lib.config import Config
from ocean_lib.data_provider.data_encryptor import DataEncryptor
from ocean_lib.data_provider.data_service_provider import DataServiceProvider
from ocean_lib.exceptions import AquariusError, ContractNotFound, InsufficientBalance
from ocean_lib.models.compute_input import ComputeInput
from ocean_lib.models.data_nft import DataNFT
from ocean_lib.models.data_nft_factory import DataNFTFactoryContract
from ocean_lib.models.datatoken import Datatoken
from ocean_lib.ocean.util import get_address_of_type
from ocean_lib.services.service import Service
from ocean_lib.structures.algorithm_metadata import AlgorithmMetadata
from ocean_lib.structures.file_objects import FilesType
from ocean_lib.utils.utilities import create_checksum
from ocean_lib.web3_internal.constants import ZERO_ADDRESS
from ocean_lib.web3_internal.currency import pretty_ether_and_wei, to_wei
from ocean_lib.web3_internal.wallet import Wallet

logger = logging.getLogger("ocean")


class OceanAssets:
    """Ocean asset class for V4."""

    @enforce_types
    def __init__(
        self, config: Config, web3: Web3, data_provider: Type[DataServiceProvider]
    ) -> None:
        """Initialises OceanAssets object."""
        self._config = config
        self._web3 = web3
        self._metadata_cache_uri = config.metadata_cache_uri
        self._data_provider = data_provider

        downloads_path = os.path.join(os.getcwd(), "downloads")
        if self._config.has_option("resources", "downloads.path"):
            downloads_path = (
                self._config.get("resources", "downloads.path") or downloads_path
            )
        self._downloads_path = downloads_path
        self._aquarius = Aquarius.get_instance(self._metadata_cache_uri)

    @enforce_types
    def validate(self, asset: Asset) -> Tuple[bool, list]:
        """
        Validate that the asset is ok to be stored in aquarius.

        :param asset: Asset.
        :return: (bool, list) list of errors, empty if valid
        """
        # Validation by Aquarius
        validation_result, validation_errors = self._aquarius.validate_asset(asset)
        if not validation_result:
            msg = f"Asset has validation errors: {validation_errors}"
            logger.error(msg)
            raise ValueError(msg)

        return validation_result, validation_errors

    @enforce_types
    def _add_defaults(
        self, services: list, datatoken: str, files: List[FilesType], provider_uri: str
    ) -> list:
        has_access_service = any(
            map(
                lambda s: s.type == ServiceTypes.ASSET_ACCESS
                and s.id == self.find_service_by_datatoken(datatoken, services),
                services,
            )
        )

        if not has_access_service:
            access_service = self.build_access_service(
                service_id="0",
                service_endpoint=self._config.provider_url,
                datatoken=datatoken,
                files=files,
            )

            services.append(access_service)

        return services

    @enforce_types
    def build_access_service(
        self,
        service_id: str,
        service_endpoint: str,
        datatoken: str,
        files: List[FilesType],
        timeout: Optional[int] = 3600,
    ) -> Service:
        return Service(
            service_id=service_id,
            service_type=ServiceTypes.ASSET_ACCESS,
            service_endpoint=service_endpoint,
            datatoken=datatoken,
            files=files,
            timeout=timeout,
        )

    @enforce_types
    def deploy_datatoken(
        self,
        data_nft_factory: DataNFTFactoryContract,
        data_nft: DataNFT,
        template_index: int,
        name: str,
        symbol: str,
        minter: str,
        fee_manager: str,
        publish_market_order_fee_address: str,
        publish_market_order_fee_token: str,
        publish_market_order_fee_amount: int,
        bytess: List[bytes],
        from_wallet: Wallet,
    ) -> str:
        tx_result = data_nft.create_erc20(
            template_index=template_index,
            name=name,
            symbol=symbol,
            minter=minter,
            fee_manager=fee_manager,
            publish_market_order_fee_address=publish_market_order_fee_address,
            publish_market_order_fee_token=publish_market_order_fee_token,
            publish_market_order_fee_amount=publish_market_order_fee_amount,
            bytess=bytess,
            from_wallet=from_wallet,
        )
        assert tx_result, "Failed to create ERC20 token."

        tx_receipt = self._web3.eth.wait_for_transaction_receipt(tx_result)
        registered_token_event = data_nft_factory.get_event_log(
            DataNFTFactoryContract.EVENT_TOKEN_CREATED,
            tx_receipt.blockNumber,
            self._web3.eth.block_number,
            None,
        )
        assert registered_token_event, "Cannot find TokenCreated event."

        return registered_token_event[0].args.newTokenAddress

    @enforce_types
    def find_service_by_datatoken(self, datatoken: str, services: list) -> str:
        return next(
            (service.id for service in services if service.datatoken == datatoken), None
        )

    @enforce_types
    def build_datatokens_list(self, services: list, deployed_datatokens: list) -> list:
        datatokens = []
        # (1-n) service per datatoken, 1 datatoken per service
        for datatoken in deployed_datatokens:
            datatokens = datatokens + [
                {
                    "address": datatoken.address,
                    "name": datatoken.contract.caller.name(),
                    "symbol": datatoken.symbol(),
                    "serviceId": service.id,
                }
                for service in services
                if service.datatoken == datatoken.address
            ]

        return datatokens

    @staticmethod
    @enforce_types
    def _encrypt_ddo(
        asset: Asset,
        provider_uri: str,
        encrypt_flag: Optional[bool] = True,
        compress_flag: Optional[bool] = True,
    ):
        # Process the DDO
        asset_dict = asset.as_dictionary()
        ddo_string = json.dumps(asset_dict, separators=(",", ":"))
        ddo_bytes = ddo_string.encode("utf-8")
        ddo_hash = create_checksum(ddo_string)

        # Plain asset
        if not encrypt_flag and not compress_flag:
            flags = bytes([0])
            document = ddo_bytes
            return document, flags, ddo_hash

        # Only compression, not encrypted
        if compress_flag and not encrypt_flag:
            flags = bytes([1])
            # Compress DDO
            document = lzma.compress(ddo_bytes)
            return document, flags, ddo_hash

        # Only encryption, not compressed
        if encrypt_flag and not compress_flag:
            flags = bytes([2])
            # Encrypt DDO
            encrypt_response = DataEncryptor.encrypt(
                objects_to_encrypt=ddo_string, provider_uri=provider_uri
            )
            document = encrypt_response.text
            return document, flags, ddo_hash

        # Encrypted & compressed
        flags = bytes([3])
        # Compress DDO
        compressed_document = lzma.compress(ddo_bytes)

        # Encrypt DDO
        encrypt_response = DataEncryptor.encrypt(
            objects_to_encrypt=compressed_document, provider_uri=provider_uri
        )

        document = encrypt_response.text

        return document, flags, ddo_hash

    @staticmethod
    @enforce_types
    def _assert_ddo_metadata(metadata: dict):
        assert isinstance(
            metadata, dict
        ), f"Expected metadata of type dict, got {type(metadata)}"

        asset_type = metadata.get("type")

        assert asset_type in (
            "dataset",
            "algorithm",
        ), f"Invalid/unsupported asset type {asset_type}"

        assert "name" in metadata, "Must have name in metadata."

    # Don't enforce types due to error:
    # TypeError: Subscripted generics cannot be used with class and instance checks
    def create(
        self,
        metadata: dict,
        publisher_wallet: Wallet,
        files: Optional[List[FilesType]] = None,
        services: Optional[list] = None,
        credentials: Optional[dict] = None,
        provider_uri: Optional[str] = None,
        data_nft_address: Optional[str] = None,
        data_nft_name: Optional[str] = None,
        data_nft_symbol: Optional[str] = None,
        data_nft_template_index: Optional[int] = 1,
        data_nft_additional_datatoken_deployer: Optional[str] = None,
        data_nft_additional_metadata_updater: Optional[str] = None,
        data_nft_uri: Optional[str] = None,
        data_nft_transferable: Optional[bool] = None,
        data_nft_owner: Optional[str] = None,
        datatoken_templates: Optional[List[int]] = None,
        datatoken_names: Optional[List[str]] = None,
        datatoken_symbols: Optional[List[str]] = None,
        datatoken_minters: Optional[List[str]] = None,
        datatoken_fee_managers: Optional[List[str]] = None,
        datatoken_publish_market_order_fee_addresses: Optional[List[str]] = None,
        datatoken_publish_market_order_fee_tokens: Optional[List[str]] = None,
        datatoken_publish_market_order_fee_amounts: Optional[List[int]] = None,
        datatoken_bytess: Optional[List[List[bytes]]] = None,
        deployed_datatokens: Optional[List[Datatoken]] = None,
        encrypt_flag: Optional[bool] = True,
        compress_flag: Optional[bool] = True,
    ) -> Optional[Asset]:
        """Register an asset on-chain.

        Creating/deploying a DataNFT contract and in the Metadata store (Aquarius).

        :param metadata: dict conforming to the Metadata accepted by Ocean Protocol.
        :param publisher_wallet: Wallet of the publisher registering this asset.
        :param files: list of files that need to be encrypted before publishing.
        :param services: list of Service objects.
        :param credentials: credentials dict necessary for the asset.
        :param provider_uri: str URL of service provider. This will be used as base to
        construct the serviceEndpoint for the `access` (download) service
        :param data_nft_address: hex str the address of the data NFT token. The new
        asset will be associated with this data NFT token address.
        :param data_nft_name: str name of data NFT token if creating a new one
        :param data_nft_symbol: str symbol of data NFT token  if creating a new one
        :param data_nft_template_index: int template index of the data NFT token, by default is 1.
        :param data_nft_additional_datatoken_deployer: str address of an additional ERC20 deployer.
        :param data_nft_additional_metadata_updater: str address of an additional metadata updater.
        :param data_nft_uri: str URL of the data NFT token.
        :param datatoken_templates: list of templates indexes for deploying datatokens if deployed_datatokens is None.
        :param datatoken_names: list of names for datatokens if deployed_datatokens is None.
        :param datatoken_symbols: list of symbols for datatokens if deployed_datatokens is None.
        :param datatoken_minters: list of minters for datatokens if deployed_datatokens is None.
        :param datatoken_fee_managers: list of fee managers for datatokens if deployed_datatokens is None.
        :param datatoken_publish_market_order_fee_addresses: list of publishing market addresses for datatokens if deployed_datatokens is None.
        :param datatoken_publish_market_order_fee_tokens: list of fee tokens for datatokens if deployed_datatokens is None.
        :param datatoken_publish_market_order_fee_amounts: list of fee values for datatokens if deployed_datatokens is None.
        :param datatoken_bytess: list of arrays of bytes for deploying datatokens, default empty (currently not used, useful for future) if deployed_datatokens is None.
        :param deployed_datatokens: list of datatokens which are already deployed.
        :param encrypt_flag: bool for encryption of the DDO.
        :param compress_flag: bool for compression of the DDO.
        :return: DDO instance
        """
        self._assert_ddo_metadata(metadata)

        if not provider_uri:
            provider_uri = DataServiceProvider.get_url(self._config)

        address = get_address_of_type(
            self._config, DataNFTFactoryContract.CONTRACT_NAME
        )
        data_nft_factory = DataNFTFactoryContract(self._web3, address)

        if not data_nft_address:
            name = data_nft_name or metadata["name"]
            symbol = data_nft_symbol or name
            additional_datatoken_deployer = (
                data_nft_additional_datatoken_deployer or ZERO_ADDRESS
            )
            additional_metadata_updater = (
                data_nft_additional_metadata_updater or ZERO_ADDRESS
            )
            token_uri = data_nft_uri or "https://oceanprotocol.com/nft/"
            transferable = data_nft_transferable or True
            owner = data_nft_owner or publisher_wallet.address
            # register on-chain
            tx_id = data_nft_factory.deploy_erc721_contract(
                name=name,
                symbol=symbol,
                template_index=data_nft_template_index,
                additional_metadata_updater=additional_metadata_updater,
                additional_datatoken_deployer=additional_datatoken_deployer,
                token_uri=token_uri,
                transferable=transferable,
                owner=owner,
                from_wallet=publisher_wallet,
            )
            tx_receipt = self._web3.eth.wait_for_transaction_receipt(tx_id)
            registered_event = data_nft_factory.get_event_log(
                DataNFTFactoryContract.EVENT_NFT_CREATED,
                tx_receipt.blockNumber,
                self._web3.eth.block_number,
                None,
            )
            data_nft_address = registered_event[0].args.newTokenAddress
            data_nft = DataNFT(self._web3, data_nft_address)
            if not data_nft:
                logger.warning("Creating new NFT failed.")
                return None
            logger.info(
                f"Successfully created NFT with address " f"{data_nft.address}."
            )
        else:
            # verify nft address
            if not data_nft_factory.verify_nft(data_nft_address):
                raise ContractNotFound(
                    f"NFT address {data_nft_address} is not found in the DataNFTFactory events."
                )

        assert (
            data_nft_address
        ), "nft_address is required for publishing a dataset asset."
        data_nft = DataNFT(self._web3, data_nft_address)

        # Create a DDO object
        asset = Asset()

        # Generating the did and adding to the ddo.
        did = (
            f"did:op:{create_checksum(data_nft.address + str(self._web3.eth.chain_id))}"
        )
        asset.did = did
        # Check if it's already registered first!
        if self._aquarius.ddo_exists(did):
            raise AquariusError(
                f"Asset id {did} is already registered to another asset."
            )
        asset.chain_id = self._web3.eth.chain_id
        asset.metadata = metadata

        asset.credentials = credentials if credentials else {"allow": [], "deny": []}

        datatoken_addresses = []
        services = services or []
        deployed_datatokens = deployed_datatokens or []

        if datatoken_names and len(datatoken_names) > 1:
            assert len(files) == len(
                datatoken_names
            ), "Files structure should be a list of files for each datatoken."

        if len(datatoken_addresses) > 1:
            assert len(files) == len(
                datatoken_addresses
            ), "Files structure should be a list of files for each datatoken."

        if len(deployed_datatokens) > 1:
            assert len(files) == len(
                deployed_datatokens
            ), "Files structure should be a list of files for each datatoken."

        if (
            len(datatoken_addresses) == 1
            or len(deployed_datatokens) == 1
            or (datatoken_names and len(datatoken_names) == 1)
        ) and (files and not isinstance(files[0], list)):
            # for the simplest case, where 1 dt is expected,
            # allow files not to be a nested list
            files = [files]

        if not deployed_datatokens:
            for datatoken_data_counter in range(len(datatoken_templates)):
                datatoken_addresses.append(
                    self.deploy_datatoken(
                        data_nft_factory=data_nft_factory,
                        data_nft=data_nft,
                        template_index=datatoken_templates[datatoken_data_counter],
                        name=datatoken_names[datatoken_data_counter],
                        symbol=datatoken_symbols[datatoken_data_counter],
                        minter=datatoken_minters[datatoken_data_counter],
                        fee_manager=datatoken_fee_managers[datatoken_data_counter],
                        publish_market_order_fee_address=datatoken_publish_market_order_fee_addresses[
                            datatoken_data_counter
                        ],
                        publish_market_order_fee_token=datatoken_publish_market_order_fee_tokens[
                            datatoken_data_counter
                        ],
                        publish_market_order_fee_amount=datatoken_publish_market_order_fee_amounts[
                            datatoken_data_counter
                        ],
                        bytess=datatoken_bytess[datatoken_data_counter],
                        from_wallet=publisher_wallet,
                    )
                )
                logger.info(
                    f"Successfully created datatoken with address "
                    f"{datatoken_addresses[-1]}."
                )
            if not services:
                for i, datatoken_address in enumerate(datatoken_addresses):
                    services = self._add_defaults(
                        services, datatoken_address, files[i], provider_uri
                    )
            for i, datatoken_address in enumerate(datatoken_addresses):
                deployed_datatokens.append(Datatoken(self._web3, datatoken_address))

            datatokens = self.build_datatokens_list(
                services=services, deployed_datatokens=deployed_datatokens
            )
        else:
            if not services:
                for i, datatoken in enumerate(deployed_datatokens):
                    services = self._add_defaults(
                        services, datatoken.address, files[i], provider_uri
                    )

            datatokens = self.build_datatokens_list(
                services=services, deployed_datatokens=deployed_datatokens
            )

        asset.nft_address = data_nft_address
        asset.datatokens = datatokens

        for service in services:
            asset.add_service(service)

        # Validation by Aquarius
        _, proof = self.validate(asset)
        proof = (
            proof["publicKey"],
            proof["v"],
            proof["r"][0],
            proof["s"][0],
        )

        document, flags, ddo_hash = self._encrypt_ddo(
            asset, provider_uri, encrypt_flag, compress_flag
        )

        data_nft.set_metadata(
            metadata_state=0,
            metadata_decryptor_url=provider_uri,
            metadata_decryptor_address=publisher_wallet.address,
            flags=flags,
            data=document,
            data_hash=ddo_hash,
            metadata_proofs=[proof],
            from_wallet=publisher_wallet,
        )

        # Fetch the asset on chain
        asset = self._aquarius.wait_for_asset(did)

        return asset

    @enforce_types
    def update(
        self,
        asset: Asset,
        publisher_wallet: Wallet,
        provider_uri: Optional[str] = None,
        encrypt_flag: Optional[bool] = True,
        compress_flag: Optional[bool] = True,
    ) -> Optional[Asset]:
        """Update an asset on-chain.

        :param asset: The updated asset to update on-chain
        :param publisher_wallet: Wallet of the publisher updating this asset.
        :param provider_uri: str URL of service provider. This will be used as base to construct the serviceEndpoint for the `access` (download) service
        :param encrypt_flag: bool for encryption of the DDO.
        :param compress_flag: bool for compression of the DDO.
        :return: Optional[Asset] the updated Asset or None if updated asset not found in metadata cache
        """

        self._assert_ddo_metadata(asset.metadata)

        if not provider_uri:
            provider_uri = DataServiceProvider.get_url(self._config)

        address = get_address_of_type(
            self._config, DataNFTFactoryContract.CONTRACT_NAME
        )
        data_nft_factory = DataNFTFactoryContract(self._web3, address)
        data_nft_address = asset.nft_address

        # Verify nft address
        if not data_nft_factory.verify_nft(data_nft_address):
            raise ContractNotFound(
                f"NFT address {data_nft_address} is not found in the DataNFTFactory events."
            )

        assert (
            data_nft_address
        ), "nft_address is required for publishing a dataset asset."
        data_nft = DataNFT(self._web3, data_nft_address)

        assert asset.chain_id == self._web3.eth.chain_id, "Chain id mismatch."

        for service in asset.services:
            service.encrypt_files(asset.nft_address)

        # Validation by Aquarius
        validation_result, errors_or_proof = self.validate(asset)
        if not validation_result:
            msg = f"Asset has validation errors: {errors_or_proof}"
            logger.error(msg)
            raise ValueError(msg)

        document, flags, ddo_hash = self._encrypt_ddo(
            asset, provider_uri, encrypt_flag, compress_flag
        )

        proof = (
            errors_or_proof["publicKey"],
            errors_or_proof["v"],
            errors_or_proof["r"][0],
            errors_or_proof["s"][0],
        )

        tx_result = data_nft.set_metadata(
            metadata_state=0,
            metadata_decryptor_url=provider_uri,
            metadata_decryptor_address=publisher_wallet.address,
            flags=flags,
            data=document,
            data_hash=ddo_hash,
            metadata_proofs=[proof],
            from_wallet=publisher_wallet,
        )

        return self._aquarius.wait_for_asset_update(asset, tx_result)

    @enforce_types
    def resolve(self, did: str) -> "Asset":
        return self._aquarius.get_asset_ddo(did)

    @enforce_types
    def search(self, text: str) -> list:
        """
        Search an asset in oceanDB using aquarius.
        :param text: String with the value that you are searching
        :return: List of assets that match with the query
        """
        logger.info(f"Searching asset containing: {text}")
        return [
            Asset.from_dict(ddo_dict["_source"])
            for ddo_dict in self._aquarius.query_search(
                {"query": {"query_string": {"query": text}}}
            )
            if "_source" in ddo_dict
        ]

    @enforce_types
    def query(self, query: dict) -> list:
        """
        Search an asset in oceanDB using search query.
        :param query: dict with query parameters
            (e.g.) https://github.com/oceanprotocol/aquarius/blob/develop/docs/for_api_users/API.md
        :return: List of assets that match with the query.
        """
        logger.info(f"Searching asset query: {query}")
        return [
            Asset.from_dict(ddo_dict["_source"])
            for ddo_dict in self._aquarius.query_search(query)
            if "_source" in ddo_dict
        ]

    @enforce_types
    def download_asset(
        self,
        asset: Asset,
        service: Service,
        consumer_wallet: Wallet,
        destination: str,
        order_tx_id: Union[str, bytes],
        index: Optional[int] = None,
        userdata: Optional[dict] = None,
    ) -> str:

        if index is not None:
            assert isinstance(index, int), logger.error("index has to be an integer.")
            assert index >= 0, logger.error("index has to be 0 or a positive integer.")

        assert (
            service and service.type == ServiceTypes.ASSET_ACCESS
        ), f"Service with type {ServiceTypes.ASSET_ACCESS} is not found."

        return download_asset_files(
            asset=asset,
            service=service,
            consumer_wallet=consumer_wallet,
            destination=destination,
            order_tx_id=order_tx_id,
            index=index,
            userdata=userdata,
        )

    @enforce_types
    def pay_for_access_service(
        self,
        asset: Asset,
        service: Service,
        consume_market_order_fee_address: str,
        consume_market_order_fee_token: str,
        consume_market_order_fee_amount: int,
        wallet: Wallet,
        consumer_address: Optional[str] = None,
    ):
        dt = Datatoken(self._web3, service.datatoken)
        balance = dt.balanceOf(wallet.address)

        if not consumer_address:
            consumer_address = wallet.address

        if balance < to_wei(1):
            raise InsufficientBalance(
                f"Your token balance {pretty_ether_and_wei(balance, dt.symbol())} is not sufficient "
                f"to execute the requested service. This service "
                f"requires {pretty_ether_and_wei(1, dt.symbol())}."
            )

        consumable_result = is_consumable(
            asset, service, {"type": "address", "value": wallet.address}
        )
        if consumable_result != ConsumableCodes.OK:
            raise AssetNotConsumable(consumable_result)

        data_provider = DataServiceProvider

        initialize_args = {
            "did": asset.did,
            "service": service,
            "consumer_address": consumer_address,
        }

        initialize_response = data_provider.initialize(**initialize_args)
        provider_fees = initialize_response.json()["providerFee"]

        tx_id = dt.start_order(
            consumer=consumer_address,
            service_index=asset.get_index_of_service(service),
            provider_fee_address=provider_fees["providerFeeAddress"],
            provider_fee_token=provider_fees["providerFeeToken"],
            provider_fee_amount=provider_fees["providerFeeAmount"],
            v=provider_fees["v"],
            r=provider_fees["r"],
            s=provider_fees["s"],
            valid_until=provider_fees["validUntil"],
            provider_data=provider_fees["providerData"],
            consume_market_order_fee_address=consume_market_order_fee_address,
            consume_market_order_fee_token=consume_market_order_fee_token,
            consume_market_order_fee_amount=consume_market_order_fee_amount,
            from_wallet=wallet,
        )

        return tx_id

    @enforce_types
    def pay_for_compute_service(
        self,
        datasets: List[ComputeInput],
        algorithm_data: Union[ComputeInput, AlgorithmMetadata],
        compute_environment: str,
        valid_until: int,
        consume_market_order_fee_address: str,
        wallet: Wallet,
        consumer_address: Optional[str] = None,
    ):
        data_provider = DataServiceProvider

        if not consumer_address:
            consumer_address = wallet.address

        initialize_response = data_provider.initialize_compute(
            [x.as_dictionary() for x in datasets],
            algorithm_data.as_dictionary(),
            datasets[0].service.service_endpoint,
            consumer_address,
            compute_environment,
            valid_until,
        )

        result = initialize_response.json()
        for i, item in enumerate(result["datasets"]):
            self._start_or_reuse_order_based_on_initialize_response(
                datasets[i],
                item,
                consume_market_order_fee_address,
                datasets[i].consume_market_order_fee_token,
                datasets[i].consume_market_order_fee_amount,
                wallet,
                consumer_address,
            )

        if "algorithm" in result:
            self._start_or_reuse_order_based_on_initialize_response(
                algorithm_data,
                result["algorithm"],
                consume_market_order_fee_address,
                algorithm_data.consume_market_order_fee_token,
                algorithm_data.consume_market_order_fee_amount,
                wallet,
                consumer_address,
            )

            return datasets, algorithm_data

        return datasets, None

    @enforce_types
    def _start_or_reuse_order_based_on_initialize_response(
        self,
        asset_compute_input: ComputeInput,
        item: dict,
        consume_market_order_fee_address: str,
        consume_market_order_fee_token: str,
        consume_market_order_fee_amount: int,
        wallet: Wallet,
        consumer_address: Optional[str] = None,
    ):
        provider_fees = item.get("providerFee")
        valid_order = item.get("validOrder")

        if valid_order and not provider_fees:
            asset_compute_input.transfer_tx_id = valid_order
            return

        service = asset_compute_input.service
        dt = Datatoken(self._web3, service.datatoken)

        if valid_order and provider_fees:
            asset_compute_input.transfer_tx_id = dt.reuse_order(
                valid_order,
                provider_fee_address=provider_fees["providerFeeAddress"],
                provider_fee_token=provider_fees["providerFeeToken"],
                provider_fee_amount=provider_fees["providerFeeAmount"],
                v=provider_fees["v"],
                r=provider_fees["r"],
                s=provider_fees["s"],
                valid_until=provider_fees["validUntil"],
                provider_data=provider_fees["providerData"],
                from_wallet=wallet,
            )
            return

        asset_compute_input.transfer_tx_id = dt.start_order(
            consumer=consumer_address,
            service_index=asset_compute_input.asset.get_index_of_service(service),
            provider_fee_address=provider_fees["providerFeeAddress"],
            provider_fee_token=provider_fees["providerFeeToken"],
            provider_fee_amount=provider_fees["providerFeeAmount"],
            v=provider_fees["v"],
            r=provider_fees["r"],
            s=provider_fees["s"],
            valid_until=provider_fees["validUntil"],
            provider_data=provider_fees["providerData"],
            consume_market_order_fee_address=consume_market_order_fee_address,
            consume_market_order_fee_token=consume_market_order_fee_token,
            consume_market_order_fee_amount=consume_market_order_fee_amount,
            from_wallet=wallet,
        )
