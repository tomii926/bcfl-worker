import torch
from hexbytes import HexBytes
from torch.utils.data import DataLoader
from web3 import Web3
from web3.types import EventData

from net import CNN_v4 as Net

from training import train

torch.backends.cudnn.benchmark = True

class Worker:
    def __init__(self, index, contract_abi, contract_address, trainset, testset, gpu_num = 0) -> None:
        self.index = index

        # training assets
        self.train_loader = DataLoader(trainset, batch_size = 128, shuffle = True, num_workers = 2, pin_memory=True)
        self.test_loader = DataLoader(testset, batch_size = 128, shuffle = False, num_workers = 2, pin_memory=True)

        self.device = torch.device(f"cuda:{gpu_num}" if torch.cuda.is_available() else "cpu")
        self.net = Net().to(self.device)
        self.optimizer = torch.optim.Adam(self.net.parameters())

        # contract
        self.w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545", request_kwargs={"timeout": 60}))
        self.account = self.w3.eth.accounts[self.index]
        self.w3.eth.default_account = self.account
        self.contract = self.w3.eth.contract(address=contract_address, abi=contract_abi)

        # constants
        self.votable_model_num = self.contract.functions.VotableModelNum().call()
        self.initial_model_cid = self.contract.functions.initialModelCID().call()

        # for simulation
        self.submitted_model_count = 0

    def register(self) -> HexBytes:
        """Register the worker to the contract."""
        
        tx_hash = self.contract.functions.register().transact({
            'gas': 1000000,
        })
        return tx_hash

    def download_net(self, CID: str) -> str:
        """与えられたCIDのモデルをダウンロードし、そのパスを返す。"""
        return f"models/{CID}.pth"

    def aggregate(self, CIDs: list) -> str:
        """与えられたCIDのモデルを平均化し、ロードする。"""
        current_net = self.net.state_dict()
        aggregated_model = current_net.copy()


        for CID in CIDs:
            model_path = self.download_net(CID)
            model = torch.load(model_path, map_location=self.device)
            for key in aggregated_model:
                aggregated_model[key] = aggregated_model[key] + (model[key] - current_net[key]) / len(CIDs)

        self.net.load_state_dict(aggregated_model)

    
    def get_CIDs_to_aggregate(self, latest_model_index: int, num_models_to_aggregate: int) -> list:
        """与えられたmodel indexから遡ってVotableModelNum個のモデルのCIDを取得する（numに満たない場合はFLの初期モデルも加える）。"""
        
        recent_model_cids =  self.get_recent_model_CIDs(latest_model_index, num_models_to_aggregate)
        if len(recent_model_cids) < num_models_to_aggregate:
            recent_model_cids = [self.initial_model_cid] + recent_model_cids

        return recent_model_cids
    
    def get_recent_model_CIDs(self, latest_model_index: int, num: int) -> list:
        """与えられたmodel indexから遡ってnum個のモデルのCIDを取得する。"""

        indices = range(max(latest_model_index - num, 0), latest_model_index)
        return [self.contract.functions.models(i).call()[0] for i in indices]

    def train(self):
        """学習を行う。"""  
        train(model=self.net, optimizer=self.optimizer, device=self.device, train_loader=self.train_loader, num_epochs=5, progress_bar=False)


    def upload_model(self):
        """upload model, returns CID."""
        cid = f"{self.index}_{self.submitted_model_count}" # for simulation. dummy value.
        torch.save(self.net.state_dict(), f"models/{cid}.pth")
        self.submitted_model_count += 1
        return cid
    

    def cids_to_vote(self, latest_model_index: int) -> list:
        votable_cids = self.get_recent_model_CIDs(latest_model_index, self.votable_model_num)
        return [votable_cids[0]] # for simulaion. dummy value.
    

    def submit(self, CID: str, cids_to_vote: list[str]) -> HexBytes:
        """Submit model CID to the contract. Returns tx_hash."""
        tx_hash = self.contract.functions.submitModel(CID, cids_to_vote).transact()
        return tx_hash
    
    def handle_event(self, event: EventData) -> HexBytes:
        """Handle event."""
        latest_model_index = event['args']['latestModelIndex']
        cids_to_aggregate = self.get_CIDs_to_aggregate(latest_model_index, self.votable_model_num)
        self.aggregate(cids_to_aggregate)
        self.train()
        cids_to_vote = self.cids_to_vote(latest_model_index)
        cid = self.upload_model()
        tx_hash = self.submit(cid, cids_to_vote)
        return tx_hash