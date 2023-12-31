#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Author: Atharva Naik (18CS10067)
import os
import json
import time
import torch
import models
import random
import pathlib
import argparse
import numpy as np
import transformers
import torch.nn as nn
from tqdm import tqdm
from torch.optim import AdamW
from typing import Union, List
import torch.nn.functional as F
from sklearn.metrics import ndcg_score as NDCG
from torch.utils.data import Dataset, DataLoader
from transformers import RobertaModel, RobertaTokenizer
from datautils.parser import remove_comments_and_docstrings
from sklearn.metrics import label_ranking_average_precision_score as MRR
from models.metrics import TripletAccuracy, RuleWiseAccuracy, recall_at_k
from models import test_ood_performance, get_tok_path, dynamic_negative_sampling, fit_disco
from models.losses import scl_loss, TripletMarginWithDistanceLoss, cos_dist, cos_cdist, cos_csim
from datautils import read_jsonl, ValRetDataset, UniBiHardNegDataset, DynamicTriplesDataset, CodeRetrieverDataset, \
CodeRetrieverTriplesDataset, CodeRetrieverQuadsDataset, CodeRetrieverQuintsDataset, batch_shuffle_collate_fn_codebert
# set logging level of transformers.
torch.autograd.set_detect_anomaly(True)
transformers.logging.set_verbosity_error()
# seed
random.seed(0)
np.random.seed(0)
torch.manual_seed(0)
# global variables. TODO: add to argparse.
# VALID_STEPS = 501
SHUFFLE_BATCH_DEBUG_SETTING = True
print(f"\x1b[31;1mUSING BATCH SHUFFLE = {SHUFFLE_BATCH_DEBUG_SETTING}\x1b[0m")
# get arguments
def get_args():
    parser = argparse.ArgumentParser("""script to train (using triplet margin loss), evaluate and predict
                                     with the CodeBERT in Late Fusion configuration for Neural Code Search.""")
    parser.add_argument("-pp", "--predict_path", type=str, default="triples/triples_train.json", help="path to data for prediction of regression scores")
    parser.add_argument("-tp", "--train_path", type=str, default="triples/triples_train.json", help="path to training triplet data")
    parser.add_argument("-vp", "--val_path", type=str, default="triples/triples_test.json", help="path to validation triplet data")
    parser.add_argument("-c", "--candidates_path", type=str, default="candidate_snippets.json", help="path to candidates (to test retrieval)")
    parser.add_argument("-q", "--queries_path", type=str, default="query_and_candidates.json", help="path to queries (to test retrieval)")
    parser.add_argument("-en", "--exp_name", type=str, default="triplet_CodeBERT_rel_thresh", help="experiment name (will be used as folder name)")
    parser.add_argument("-d", "--device_id", type=str, default="cpu", help="device string (GPU) for doing training/testing")
    parser.add_argument("-als", "--all_losses_setting", action="store_true", help="use all possible losses")
    parser.add_argument("-disco", "--disco_baseline", action="store_true", help="use DISCO training procedure")
    # parser.add_argument("-tec", "--test_cls", action="store_true", help="")
    # parser.add_argument("-tc", "--train_cls", action="store_true", help="")
    # parser.add_argument("-ter", "--test_rel", action="store_true", help="")
    # parser.add_argument("-tr", "--train_rel", action="store_true", help="")
    parser.add_argument("-scl", "--use_scl", action="store_true", help="use selectively contrastive learning")
    parser.add_argument("-ccl", "--use_ccl", action="store_true", help="use code contrastive loss for hard negatives")
    parser.add_argument("-lr", "--lr", type=float, default=1e-5, help="learning rate for training (defaults to 1e-5)")
    parser.add_argument("-te", "--test", action="store_true", help="flag to do testing")
    parser.add_argument("-t", "--train", action="store_true", help="flag to do training")
    parser.add_argument("-bs", "--batch_size", type=int, default=32, help="batch size")
    parser.add_argument("-e", "--epochs", type=int, default=5, help="no. of epochs")
    parser.add_argument("-dns", "--dynamic_negative_sampling", action="store_true", 
                        help="do dynamic negative sampling at batch level")
    parser.add_argument("-sip", "--sim_intents_path", type=str, default=None, 
                        help="path to dictionary containing similar intents corresponding to a given intent")
    parser.add_argument("-pcp", "--perturbed_codes_path", type=str, default=None, 
                        help="path to dictionary containing AST perturbed codes corresponding to a given code")
    parser.add_argument("-csp", "--code_syns_path", type=str, default=None, 
                        help="path to code synsets for all losses setting")
    parser.add_argument("-ccpp", "--code_code_pairs_path", type=str, default=None, 
                        help="path to code-code pairs for CodeRetriever's unimodal objective")
    parser.add_argument("-w", "--warmup_steps", type=int, default=3000, help="no. of warmup steps (soft negatives only during warmup)")
    parser.add_argument("-p", "--p", type=int, default=2, help="the p used in mastering rate")
    parser.add_argument("-beta", "--beta", type=float, default=0.01, help="the beta used in the von-Mises fisher sampling")
    parser.add_argument("-ast", "--use_AST", action="store_true", help="use AST perturbed negative samples")
    parser.add_argument("-crb", "--code_retriever_baseline", action="store_true", help="use CodeRetriever objective")
    parser.add_argument("-crq", "--code_retriever_quad", action="store_true", help="use CodeRetriever objective with quads")
    parser.add_argument("-crq5", "--code_retriever_quint", action="store_true", help="use CodeRetriever objective with quints")
    parser.add_argument("-crt", "--code_retriever_triplets", action="store_true", help="use CodeRetriever bimodal objective with random triplets")
    parser.add_argument("-crml", "--code_retriever_ml_loss", action="store_true", help="use triplet margin loss with CodeRetriever objective")
    parser.add_argument("-crsu", "--code_retriever_skip_unimodal", action="store_true", help="skip the unimodal loss in the CodeRetriever objective")
    parser.add_argument("-idns", "--intent_level_dynamic_sampling", action="store_true", 
                        help="dynamic sampling based on similar intents")
    parser.add_argument("-vs", "--valid_steps", type=int, default=501, help="validation step interval")
    parser.add_argument("-nc", "--no_curriculum", action="store_true", help="turn of curriclum (only hard negatives)")
    parser.add_argument("-uce", "--use_cross_entropy", action="store_true", help="use cross entropy loss instead of triplet margin loss")
    parser.add_argument("-ct", "--curr_type", type=str, default="mr", choices=['mr', 'rand', 'lp', 'exp', 'hard', "soft"],
                        help="""type of curriculum (listed below): 
                             1) mr: mastering rate based curriculum 
                             2) rand: equal prob. of hard & soft -ves
                             3) lp: learning progress based curriculum
                             4) exp: exponential decay with steps/epochs
                             5) hard: hard negatives only
                             6) soft: soft negatives only""")
    parser.add_argument("-igwr", "--ignore_worst_rules", action='store_true',
                        help="ignore the 6 worst/easiest perturbation rules")
    parser.add_argument("-igowr", "--ignore_old_worst_rules", action='store_true',
                        help="ignore the 4 worst/easiest perturbation rules from the old set")
    parser.add_argument("-ignr", "--ignore_new_rules", action='store_true',
                        help="ignore the newly created rules")
    parser.add_argument("-igur", "--ignore_unnatural_rules", action='store_true',
                        help="ignore unnatural rule types")
    parser.add_argument("-discr", "--use_disco_rules", action='store_true',
                        help="use the rules outlined in/inspired by the DISCO paper (9)")
    parser.add_argument("-too", "--test_ood", action="store_true", help="flat to do ood testing")
    parser.add_argument("-cexp", "--comb_exp", action="store_true", help="experimenal combined loss setting")
    parser.add_argument("-csim", "--use_csim", action="store_true", help="cosine similarity instead of euclidean distance")
    args = parser.parse_args()
    if args.use_cross_entropy and args.curr_type not in ["soft", "hard"]:
        args.curr_type = "hard"
    if args.use_ccl: args.curr_type = "hard"
    assert not(args.use_ccl and args.use_cross_entropy), "conflicting objectives selected: CCL and CE CL"
    assert not(args.use_ccl and args.code_retriever_baseline), "conflicting objectives selected: CCL and CodeRetriever"
    if args.code_retriever_baseline: # only use soft negative for CodeRetriever
        args.curr_type = "soft"
    if args.code_retriever_triplets:
        args.code_retriever_skip_unimodal = True
    # parser.add_argument("-cp", "--ckpt_path", type=str, default="triplet_CodeBERT_rel_thresh/model.pt")
    return args

# TripletMarginWithDistanceLoss for custom design function.
class CodeDataset(Dataset):
    def __init__(self, code_snippets: str, tokenizer: Union[str, None, RobertaTokenizer]=None, **tok_args):
        super(CodeDataset, self).__init__()
        self.data = code_snippets
        self.tok_args = tok_args
        if isinstance(tokenizer, RobertaTokenizer):
            self.tokenizer = tokenizer
        elif isinstance(tokenizer, str):
            self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer = tokenizer
    
    def __len__(self):
        return len(self.data)
    
    def proc_code(self, code: str):
        try: code = remove_comments_and_docstrings(code, 'python')
        except: pass
        code = " ".join(code.split("\n")).strip()
        return code
    
    def __getitem__(self, i: int):
        code = self.proc_code(self.data[i])
        if self.tokenizer:
            # special tokens are added by default.
            code = self.tokenizer(code, **self.tok_args)            
            return [code["input_ids"][0], 
                    code["attention_mask"][0]]
        else:
            return [code]
        
        
class TextDataset(Dataset):
    def __init__(self, texts: str, tokenizer: Union[str, None, RobertaTokenizer]=None, **tok_args):
        super(TextDataset, self).__init__()
        self.data = texts
        self.tok_args = tok_args
        if isinstance(tokenizer, RobertaTokenizer):
            self.tokenizer = tokenizer
        elif isinstance(tokenizer, str):
            self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer = tokenizer
    
    def __len__(self):
        return len(self.data)
    
    def proc_text(self, text: str):
        text = " ".join(text.split("\n"))
        text = " ".join(text.split()).strip()
        return text
    
    def __getitem__(self, i: int):
        text = self.proc_text(self.data[i])
        if self.tokenizer:
            # special tokens are added by default.
            text = self.tokenizer(text, **self.tok_args)            
            return [text["input_ids"][0], 
                    text["attention_mask"][0]]
        else:
            return [text]      
# class RelevanceClassifierDataset(Dataset):
#     def __init__(self, path: str, thresh: float=0.06441,
#                  tokenizer: Union[str, None, RobertaTokenizer]=None, 
#                  **tok_args):
#         super(RelevanceClassifierDataset, self).__init__()
#         self.data = json.load(open(path))
#         self.tok_args = tok_args
#         if isinstance(tokenizer, RobertaTokenizer):
#             self.tokenizer = tokenizer
#         elif isinstance(tokenizer, str):
#             self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
#         else:
#             self.tokenizer = tokenizer
#         self.thresh = thresh
    
#     def __len__(self):
#         return len(self.data)
    
#     def proc_code(self, code: str):
#         try: code = remove_comments_and_docstrings(code, 'python')
#         except: pass
#         code = " ".join(code.split("\n")).strip()
#         return code
    
#     def proc_text(self, text: str):
#         text = " ".join(text.split("\n"))
#         text = " ".join(text.split()).strip()
#         return text
    
#     def __getitem__(self, i: int):
#         try:
#             text = self.proc_text(self.data[i]["intent"])
#             code = self.proc_code(self.data[i]["snippet"])
#             label = 1 if self.data[i]["prob"] >= self.thresh else 0
#         except TypeError:
#             print(self.data[i])
#         if self.tokenizer:
#             # special tokens are added by default.
#             text_n_code = self.tokenizer(text, code, **self.tok_args)
#             return [text_n_code["input_ids"][0], 
#                     text_n_code["attention_mask"][0],
#                     torch.as_tensor(label)]
#         else:
#             return [text_n_code, label]
# class RelevanceRegressionDataset(Dataset):
#     def __init__(self, path: str, 
#                  tokenizer: Union[str, None, RobertaTokenizer]=None, 
#                  **tok_args):
#         super(RelevanceRegressionDataset, self).__init__()
#         self.data = json.load(open(path))
#         self.tok_args = tok_args
#         if isinstance(tokenizer, RobertaTokenizer):
#             self.tokenizer = tokenizer
#         elif isinstance(tokenizer, str):
#             self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
#         else:
#             self.tokenizer = tokenizer
    
#     def __len__(self):
#         return len(self.data)
    
#     def proc_code(self, code: str):
#         try: code = remove_comments_and_docstrings(code, 'python')
#         except: pass
#         code = " ".join(code.split("\n")).strip()
#         return code
    
#     def proc_text(self, text: str):
#         text = " ".join(text.split("\n"))
#         text = " ".join(text.split()).strip()
#         return text
    
#     def __getitem__(self, i: int):
#         try:
#             text = self.proc_text(self.data[i]["intent"])
#             code = self.proc_code(self.data[i]["snippet"])
#             label = float(self.data[i]["prob"])
#         except TypeError:
#             print(self.data[i])
#         if self.tokenizer:
#             # special tokens are added by default.
#             text_n_code = self.tokenizer(text, code, **self.tok_args)
#             return [text_n_code["input_ids"][0], 
#                     text_n_code["attention_mask"][0],
#                     torch.as_tensor(label)]
#         else:
#             return [text_n_code, label]  
class TextCodePairDataset(Dataset):
    def __init__(self, query_candidate_pairs: str, 
                 tokenizer: Union[str, None, RobertaTokenizer]=None, 
                 **tok_args):
        super(TextCodePairDataset, self).__init__()
        self.data = query_candidate_pairs
        self.tok_args = tok_args
        if isinstance(tokenizer, RobertaTokenizer):
            self.tokenizer = tokenizer
        elif isinstance(tokenizer, str):
            self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer = tokenizer
    
    def __len__(self):
        return len(self.data)
    
    def proc_code(self, code: str):
        try: code = remove_comments_and_docstrings(code, 'python')
        except: pass
        code = " ".join(code.split("\n")).strip()
        return code
    
    def proc_text(self, text: str):
        text = " ".join(text.split("\n"))
        text = " ".join(text.split()).strip()
        return text
    
    def __getitem__(self, i: int):
        text = self.proc_text(self.data[i][0])
        code = self.proc_code(self.data[i][1])
        if self.tokenizer:
            # special tokens are added by default.
            text_n_code = self.tokenizer(text, code, **self.tok_args)
            return [text_n_code["input_ids"][0], 
                    text_n_code["attention_mask"][0]]
        else:
            return [text_n_code]
    
    
class TriplesDataset(Dataset):
    def __init__(self, path: str, tokenizer: Union[str, None, RobertaTokenizer]=None, **tok_args):
        super(TriplesDataset, self).__init__()
        self.data = json.load(open(path))
        self.tok_args = tok_args
        if isinstance(tokenizer, RobertaTokenizer):
            self.tokenizer = tokenizer
        elif isinstance(tokenizer, str):
            self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer = tokenizer
        
    def __len__(self):
        return len(self.data)
    
    def proc_text(self, text: str):
        text = " ".join(text.split("\n"))
        text = " ".join(text.split()).strip()
        return text
    
    def proc_code(self, code: str):
        try: code = remove_comments_and_docstrings(code, 'python')
        except: pass
        code = " ".join(code.split("\n")).strip()
        return code
        
    def __getitem__(self, i: int):
        anchor = self.proc_text(self.data[i][0])
        pos = self.proc_code(self.data[i][1])
        neg = self.proc_code(self.data[i][2])
        if self.tokenizer:
            # special tokens are added by default.
            anchor = self.tokenizer(anchor, **self.tok_args)
            pos = self.tokenizer(pos, **self.tok_args)
            neg = self.tokenizer(neg, **self.tok_args)
            
            return [
                    anchor["input_ids"][0], anchor["attention_mask"][0], 
                    pos["input_ids"][0], pos["attention_mask"][0],
                    neg["input_ids"][0], neg["attention_mask"][0],
                   ]
        else:
            return [anchor, pos, neg]      
# class CodeBERTRelevanceClassifier(nn.Module):
#     """
#     finetune CodeBERT over CoNaLa mined pairs 
#     for predicting relevance score using regression
#     """
#     def __init__(self, model_path: str="microsoft/codebert-base", 
#                  tok_path: str="microsoft/codebert-base", **args):
#         super(CodeBERTRelevanceClassifier, self).__init__()
#         self.config = {}
#         self.config["tok_path"] = tok_path
#         self.config["model_path"] = model_path
        
#         print(f"loading pretrained CodeBERT embedding model from {model_path}")
#         start = time.time()
#         self.model = RobertaModel.from_pretrained(model_path)
#         print(f"loaded CodeBERT model in {(time.time()-start):.2f}s")
#         print(f"loaded tokenizer files from {tok_path}")
#         self.mlp = nn.Linear(768, 1)
#         self.sigmoid = nn.Sigmoid()
#         self.tokenizer = RobertaTokenizer.from_pretrained(tok_path)
#         # optimizer and loss.
#         adam_eps = 1e-8
#         lr = args.get("lr", 1e-5)
#         self.config["lr"] = lr
        
#         print(f"optimizer = AdamW(lr={lr}, eps={adam_eps})")
#         self.optimizer = AdamW(self.parameters(), eps=adam_eps, lr=lr)
#         self.config["optimizer"] = f"{self.optimizer}"
        
#         self.loss_fn = nn.BCELoss()
#         print(f"loss_fn = {self.loss_fn}")
#         self.config["loss_fn"] = f"{self.loss_fn}"
        
#     def forward(self, text_code_pair_args):
#         # text_code_pair_args: ids, attn_mask with "[CLS] <text> [SEP] <code> [SEP]"
#         text_code_pair_embed = self.model(*text_code_pair_args).pooler_output # (batch, emb_size)
#         # print("text_code_pair_embed.device =", text_code_pair_embed.device)
#         # x = self.mlp(text_code_pair_embed)
#         # print("x.device =", x.device)
#         # x = self.sigmoid(x)
#         return self.sigmoid(self.mlp(text_code_pair_embed))
# #     def predict(self, q_and_c, **args):
# #         queries_and_candidates = [] 
# #         batch_size = args.get("batch_size", 32)
# #         device = args.get("device") if torch.cuda.is_available() else "cpu"
# #         dataset = TextCodePairDataset(q_and_c, tokenizer=self.tokenizer,
# #                                       truncation=True, padding="max_length",
# #                                       max_length=200, add_special_tokens=True,
# #                                       return_tensors="pt")
# #         dataloader = DataLoader(dataset, shuffle=False, 
# #                                 batch_size=batch_size)
# #         relevance_scores = []
# #         pbar = tqdm(enumerate(dataloader), total=len(dataloader), desc="predicting relevance")
# #         for step, batch in pbar:
# #             with torch.no_grad():
# #                 text_code_pair = (batch[0].to(device), batch[1].to(device))
# #                 pred_reg_score = self(text_code_pair).squeeze().tolist()
# #                 relevance_scores += pred_reg_score
# #                 # if step == 5: break # DEBUG
# #         return relevance_scores
#     def val(self, valloader: DataLoader, epoch_i: int=0, epochs: int=0, device="cuda:0"):
#         self.eval()
#         batch_losses = []
#         pbar = tqdm(enumerate(valloader), total=len(valloader), 
#                     desc=f"val: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0")
#         tot = 0
#         matches = 0
#         for step, batch in pbar:
#             with torch.no_grad():
#                 text_code_pair = (batch[0].to(device), batch[1].to(device))
#                 rel_label = batch[2].float().to(device) # 0 or 1.
#                 pred_probs = self(text_code_pair).squeeze()
#                 batch_loss = self.loss_fn(pred_probs, rel_label)
#                 batch_losses.append(batch_loss.item())
#                 tot += len(rel_label)
#                 matches += ((pred_probs > 0.06441).float() == rel_label).sum().item()
#                 acc = (matches/tot)
#                 pbar.set_description(f"val: epoch: {epoch_i+1}/{epochs} acc: {100*acc:.2f} batch_loss: {batch_loss:.5f} loss: {np.mean(batch_losses):.5f}")
#                 # if step == 5: break # DEBUG
#         return np.mean(batch_losses)
    
#     def fit(self, train_path: str, val_path: str, **args):
#         thresh: float = 0.06441
#         batch_size = args.get("batch_size", 32)
#         epochs = args.get("epochs", 5)
#         device_id = args.get("device_id", "cuda:0")
#         device = device_id if torch.cuda.is_available() else "cpu"
#         exp_name = args.get("exp_name", "experiment")
#         os.makedirs(exp_name, exist_ok=True)
#         save_path = os.path.join(exp_name, "model.pt")
#         # store config info.
#         self.config["batch_size"] = batch_size
#         self.config["train_path"] = train_path
#         self.config["device_id"] = device_id
#         self.config["exp_name"] = exp_name
#         self.config["val_path"] = val_path
#         self.config["epochs"] = epochs
        
#         config_path = os.path.join(exp_name, "config.json")
#         with open(config_path, "w") as f:
#             json.dump(self.config, f)
#         print(f"saved config to {config_path}")
#         print(f"model will be saved at {save_path}")
#         print(f"moving model to {device}")
#         self.to(device)
#         trainset = RelevanceClassifierDataset(train_path, tokenizer=self.tokenizer,
#                                               truncation=True, padding="max_length",
#                                               max_length=200, add_special_tokens=True,
#                                               return_tensors="pt", thresh=0.06441)
#         valset = RelevanceClassifierDataset(val_path, tokenizer=self.tokenizer,
#                                             truncation=True, padding="max_length",
#                                             max_length=200, add_special_tokens=True,
#                                             return_tensors="pt", thresh=0.06441)
#         trainloader = DataLoader(trainset, shuffle=True, 
#                                  batch_size=batch_size)
#         valloader = DataLoader(valset, shuffle=False,
#                                batch_size=batch_size)
#         train_metrics = {
#             "epochs": [],
#             "summary": [],
#         } 
#         best_val_loss = 100
#         for epoch_i in range(epochs):
#             tot = 0
#             matches = 0
#             self.train()
#             batch_losses = []
#             pbar = tqdm(enumerate(trainloader), total=len(trainloader),
#                         desc=f"train: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0")
#             for step, batch in pbar:      
#                 text_code_pair = (batch[0].to(device), batch[1].to(device))
#                 rel_label = batch[2].float().to(device)
#                 pred_probs = self(text_code_pair).squeeze()
#                 # print(true_reg_score)
#                 # print(pred_reg_score.device)
#                 batch_loss = self.loss_fn(pred_probs, rel_label)
#                 batch_loss.backward()
#                 self.optimizer.step()
#                 # scheduler.step()  # Update learning rate schedule
#                 self.zero_grad()
#                 batch_losses.append(batch_loss.item())
#                 tot += len(rel_label)
#                 matches += ((pred_probs > 0.06441).float() == rel_label).sum().item()
#                 acc = (matches/tot)
#                 pbar.set_description(f"train: epoch: {epoch_i+1}/{epochs} acc: {100*acc:.2f} batch_loss: {batch_loss:.5f} loss: {np.mean(batch_losses):.5f}")
#                 # if step == 5: break # DEBUG
#             # validate current model
#             val_loss = self.val(valloader, epoch_i=epoch_i, 
#                                 epochs=epochs, device=device)
#             if val_loss < best_val_loss:
#                 print(f"saving best model till now with val_loss: {val_loss} at {save_path}")
#                 best_val_loss = val_loss
#                 torch.save(self.state_dict(), save_path)
#             train_metrics["epochs"].append({
#                 "train_batch_losses": batch_losses, 
#                 "train_loss": np.mean(batch_losses), 
#                 "val_loss": val_loss,
#             })
        
#         return train_metrics
    
    
# class CodeBERTRelevanceRegressor(nn.Module):
#     """
#     finetune CodeBERT over CoNaLa mined pairs 
#     for predicting relevance score using regression
#     """
#     def __init__(self, model_path: str="microsoft/codebert-base", 
#                  tok_path: str="microsoft/codebert-base", **args):
#         super(CodeBERTRelevanceRegressor, self).__init__()
#         self.config = {}
#         self.config["model_path"] = model_path
#         self.config["tok_path"] = tok_path
        
#         print(f"loading pretrained CodeBERT embedding model from {model_path}")
#         start = time.time()
#         self.model = RobertaModel.from_pretrained(model_path)
#         print(f"loaded CodeBERT model in {(time.time()-start):.2f}s")
#         print(f"loaded tokenizer files from {tok_path}")
#         self.mlp = nn.Linear(768, 1)
#         self.sigmoid = nn.Sigmoid()
#         self.tokenizer = RobertaTokenizer.from_pretrained(tok_path)
#         # optimizer and loss.
#         adam_eps = 1e-8
#         lr = args.get("lr", 1e-5)
#         self.config["lr"] = lr
        
#         print(f"optimizer = AdamW(lr={lr}, eps={adam_eps})")
#         self.optimizer = AdamW(self.parameters(), eps=adam_eps, lr=lr)
#         self.config["optimizer"] = f"{self.optimizer}"
        
#         self.loss_fn = nn.MSELoss()
#         print(f"loss_fn = {self.loss_fn}")
#         self.config["loss_fn"] = f"{self.loss_fn}"
        
#     def forward(self, text_code_pair_args):
#         # text_code_pair_args: ids, attn_mask with "[CLS] <text> [SEP] <code> [SEP]"
#         text_code_pair_embed = self.model(*text_code_pair_args).pooler_output # (batch, emb_size)
#         # print("text_code_pair_embed.device =", text_code_pair_embed.device)
#         # x = self.mlp(text_code_pair_embed)
#         # print("x.device =", x.device)
#         # x = self.sigmoid(x)
#         return self.sigmoid(self.mlp(text_code_pair_embed))
    
#     def predict(self, q_and_c, **args):
#         queries_and_candidates = [] 
#         batch_size = args.get("batch_size", 32)
#         device = args.get("device") if torch.cuda.is_available() else "cpu"
#         dataset = TextCodePairDataset(q_and_c, tokenizer=self.tokenizer,
#                                       truncation=True, padding="max_length",
#                                       max_length=200, add_special_tokens=True,
#                                       return_tensors="pt")
#         dataloader = DataLoader(dataset, shuffle=False, 
#                                 batch_size=batch_size)
#         relevance_scores = []
#         pbar = tqdm(enumerate(dataloader), total=len(dataloader), desc="predicting relevance")
#         for step, batch in pbar:
#             with torch.no_grad():
#                 text_code_pair = (batch[0].to(device), batch[1].to(device))
#                 pred_reg_score = self(text_code_pair).squeeze().tolist()
#                 relevance_scores += pred_reg_score
#                 # if step == 5: break # DEBUG
#         return relevance_scores

#     def val(self, valloader: DataLoader, epoch_i: int=0, epochs: int=0, device="cuda:0"):
#         self.eval()
#         batch_losses = []
#         pbar = tqdm(enumerate(valloader), total=len(valloader), 
#                     desc=f"val: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0")
#         for step, batch in pbar:
#             with torch.no_grad():
#                 text_code_pair = (batch[0].to(device), batch[1].to(device))
#                 true_reg_score = batch[2].to(device)
#                 pred_reg_score = self(text_code_pair).squeeze()
#                 batch_loss = self.loss_fn(pred_reg_score, true_reg_score)
#                 batch_losses.append(batch_loss.item())
#                 pbar.set_description(f"val: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.5f} loss: {np.mean(batch_losses):.5f}")
#                 # if step == 5: break # DEBUG
#         return np.mean(batch_losses)
    
#     def fit(self, train_path: str, val_path: str, **args):
#         batch_size = args.get("batch_size", 32)
#         self.config["batch_size"] = batch_size
#         epochs = args.get("epochs", 5)
#         self.config["epochs"] = epochs
#         device_id = args.get("device_id", "cuda:0")
#         self.config["device_id"] = device_id
#         device = device_id if torch.cuda.is_available() else "cpu"
#         exp_name = args.get("exp_name", "experiment")
#         self.config["exp_name"] = exp_name
#         os.makedirs(exp_name, exist_ok=True)
#         save_path = os.path.join(exp_name, "model.pt")
#         self.config["train_path"] = train_path
#         self.config["val_path"] = val_path
        
#         config_path = os.path.join(exp_name, "config.json")
#         with open(config_path, "w") as f:
#             json.dump(self.config, f)
#         print(f"saved config to {config_path}")
#         print(f"model will be saved at {save_path}")
#         print(f"moving model to {device}")
#         self.to(device)
#         trainset = RelevanceRegressionDataset(train_path, tokenizer=self.tokenizer,
#                                               truncation=True, padding="max_length",
#                                               max_length=200, add_special_tokens=True,
#                                               return_tensors="pt")
#         valset = RelevanceRegressionDataset(val_path, tokenizer=self.tokenizer,
#                                             truncation=True, padding="max_length",
#                                             max_length=200, add_special_tokens=True,
#                                             return_tensors="pt")
#         trainloader = DataLoader(trainset, shuffle=True, 
#                                  batch_size=batch_size)
#         valloader = DataLoader(valset, shuffle=False,
#                                batch_size=batch_size)
#         train_metrics = {
#             "epochs": [],
#             "summary": [],
#         } 
#         best_val_loss = 100
#         for epoch_i in range(epochs):
#             self.train()
#             batch_losses = []
#             pbar = tqdm(enumerate(trainloader), total=len(trainloader),
#                         desc=f"train: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0")
#             for step, batch in pbar:      
#                 text_code_pair = (batch[0].to(device), batch[1].to(device))
#                 true_reg_score = batch[2].to(device)
#                 pred_reg_score = self(text_code_pair).squeeze()
#                 # print(true_reg_score)
#                 # print(pred_reg_score.device)
#                 batch_loss = self.loss_fn(pred_reg_score, true_reg_score)
#                 batch_loss.backward()
#                 self.optimizer.step()
#                 # scheduler.step()  # Update learning rate schedule
#                 self.zero_grad()
#                 batch_losses.append(batch_loss.item())
#                 pbar.set_description(f"train: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.5f} loss: {np.mean(batch_losses):.5f}")
#                 # if step == 5: break # DEBUG
#             # validate current model
#             val_loss = self.val(valloader, epoch_i=epoch_i, 
#                                          epochs=epochs, device=device)
#             if val_loss < best_val_loss:
#                 print(f"saving best model till now with val_loss: {val_loss} at {save_path}")
#                 best_val_loss = val_loss
#                 torch.save(self.state_dict(), save_path)
#             train_metrics["epochs"].append({
#                 "train_batch_losses": batch_losses, 
#                 "train_loss": np.mean(batch_losses), 
#                 "val_loss": val_loss,
#             })
        
#         return train_metrics
class CodeBERTripletNet(nn.Module):
    """ Class to 
    1) finetune CodeBERT in a late fusion setting using triplet margin loss.
    2) Evaluate metrics on unseen test set.
    3) 
    """
    def __init__(self, model_path: str="microsoft/codebert-base", 
                 tok_path: str="microsoft/codebert-base", **args):
        super(CodeBERTripletNet, self).__init__()
        self.pdist = nn.PairwiseDistance()
        self.config = {}
        self.config["model_path"] = model_path
        self.config["tok_path"] = tok_path

        margin = args.get("margin", 1)
        dist_fn_deg = args.get("dist_fn_deg", 2)

        self.ignore_new_rules = args.get("ignore_new_rules", False)
        self.ignore_worst_rules = args.get("ignore_worst_rules", False)
        self.ignore_non_disco_rules = args.get("use_disco_rules", False)
        self.ignore_old_worst_rules = args.get("ignore_old_worst_rules", False)
        self.ignore_unnatural_rules = args.get("ignore_unnatural_rules", False)
        self.code_retriever_ml_loss = args.get("code_retriever_ml_loss", False)
        self.code_retriever_baseline = args.get("code_retriever_baseline", False)
        self.code_retriever_triplets = args.get("code_retriever_triplets", False)
        self.code_retriever_skip_unimodal = args.get("code_retriever_skip_unimodal", False)
        self.use_cross_entropy = args.get("use_cross_entropy", False)
        self.use_ccl = args.get("use_ccl", False)
        self.use_scl = args.get("use_scl", False)
        self.use_csim = args.get("use_csim", False)
        self.comb_exp = args.get("comb_exp", False)
        
        print(f"loading pretrained CodeBERT embedding model from {model_path}")
        start = time.time()
        self.embed_model = RobertaModel.from_pretrained(model_path)
        print(f"loaded embedding model in {(time.time()-start):.2f}s")
        print(f"loaded tokenizer files from {tok_path}")
        self.tokenizer = RobertaTokenizer.from_pretrained(tok_path)
        # optimizer and loss.
        adam_eps = 1e-8
        lr = args.get("lr", 1e-5)
        self.config["lr"] = lr
        print(f"optimizer = AdamW(lr={lr}, eps={adam_eps})")
        self.optimizer = AdamW(self.parameters(), eps=adam_eps, lr=lr)
        # print(f"loss_fn = TripletMarginLoss(margin={margin}, p={dist_fn_deg})")
        self.dropout1 = nn.Dropout(0.1)
        self.dropout2 = nn.Dropout(0.1)
        self.ce_loss = nn.CrossEntropyLoss()
        # elif self.use_scl: self.loss_fn = TripletMarginWithDistanceLoss(
        #     margin=margin, distance_function=cos_dist,
        # )
        self.loss_fn = nn.TripletMarginLoss(
            margin=margin, p=dist_fn_deg, 
            reduction="none",
        )
        self.config["code_retriever_skip_unimodal"] = self.code_retriever_skip_unimodal
        self.config["code_retriever_triplets"] = self.code_retriever_triplets
        self.config["code_retriever_baseline"] = self.code_retriever_baseline
        self.config["code_retriever_ml_loss"] = self.code_retriever_ml_loss
        self.config["ingore_unnatural_rules"] = self.ignore_unnatural_rules
        self.config["ignore_old_worst_rules"] = self.ignore_old_worst_rules
        self.config["ignore_worst_rules"] = self.ignore_worst_rules
        self.config["ignore_new_rules"] = self.ignore_new_rules
        self.config["use_cross_entropy"] = self.use_cross_entropy
        self.config["use_disco_rules"] = self.ignore_non_disco_rules
        self.config["dist_fn_deg"] = dist_fn_deg
        self.config["optimizer"] = f"{self.optimizer}"
        self.config["comb_exp"] = self.comb_exp
        self.config["use_csim"] = self.use_csim
        self.config["loss_fn"] = f"{self.loss_fn}"
        self.config["use_scl"] = self.use_scl
        self.config["use_ccl"] = self.use_ccl
        self.config["margin"] = margin
        
    def val_ret(self, valset: Dataset, device="cuda:0"):
        self.eval()
        # get queries and candidates from validation set and encode them.
        labels = valset.get_labels()
        queries = valset.get_queries()
        candidates = valset.get_candidates()
        print(f"encoding {len(queries)} queries:")
        query_mat = self.encode_emb(queries, mode="text", batch_size=48,
                                    use_tqdm=True, device_id=device)
        query_mat = torch.stack(query_mat)
        print(f"encoding {len(candidates)} candidates:")
        cand_mat = self.encode_emb(candidates, mode="code", batch_size=48,
                                   use_tqdm=True, device_id=device)
        # score and rank documents.
        cand_mat = torch.stack(cand_mat)
        # if self.use_scl: 
        # scores = -(query_mat @ cand_mat.T)
        # scores = cos_cdist(query_mat, cand_mat)
        #else: \
        if self.use_csim: scores = -cos_csim(query_mat, cand_mat)
        else: scores = torch.cdist(query_mat, cand_mat, p=2)
        doc_ranks = scores.argsort(axis=1)
        recall_at_5 = recall_at_k(labels, doc_ranks.tolist(), k=5)
        
        return recall_at_5
        
    def forward(self, anchor_title, pos_snippet, neg_snippet):
        anchor_text_emb = self.embed_model(*anchor_title).pooler_output # get [CLS] token (batch, emb_size)
        pos_code_emb = self.embed_model(*pos_snippet).pooler_output # get [CLS] token (batch, emb_size)
        neg_code_emb = self.embed_model(*neg_snippet).pooler_output # get [CLS] token (batch, emb_size)
        
        return anchor_text_emb, pos_code_emb, neg_code_emb
        
    def val(self, valloader: DataLoader, epoch_i: int=0, epochs: int=0, device="cuda:0"):
        self.eval()
        val_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
        batch_losses = []
        pbar = tqdm(enumerate(valloader), total=len(valloader), 
                    desc=f"val: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0 acc: 0")
        for step, batch in pbar:
            with torch.no_grad():
                anchor_title = (batch[0].to(device), batch[1].to(device))
                pos_snippet = (batch[2].to(device), batch[3].to(device))
                neg_snippet = (batch[4].to(device), batch[5].to(device))
                anchor_text_emb, pos_code_emb, neg_code_emb = self(anchor_title, pos_snippet, neg_snippet)
                batch_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb).mean()
                val_acc.update(anchor_text_emb, pos_code_emb, neg_code_emb)
                batch_losses.append(batch_loss.item())
                pbar.set_description(f"val: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.3f} loss: {np.mean(batch_losses):.3f} acc: {100*val_acc.get():.2f}")
                # if step == 5: break # DEBUG
        return val_acc.get(), np.mean(batch_losses)
        
    def encode_emb(self, text_or_snippets: List[str], mode: str="text", **args) -> list:
        """Note: our late fusion CodeBERT is a universal encoder for text and code, so the same function works for both."""
        device_id = args.get("device_id", "cuda:0")
        batch_size = args.get("batch_size", 32)
        use_tqdm = args.get("use_tqdm", False)
        
        device = device_id if torch.cuda.is_available() else "cpu"
        self.to(device)
        self.eval()
        
        if mode == "text":
            dataset = TextDataset(text_or_snippets, tokenizer=self.tokenizer,
                                  truncation=True, padding="max_length",
                                  max_length=100, add_special_tokens=True,
                                  return_tensors="pt")
        elif mode == "code":
            dataset = CodeDataset(text_or_snippets, tokenizer=self.tokenizer,
                                  truncation=True, padding="max_length",
                                  max_length=100, add_special_tokens=True,
                                  return_tensors="pt")
        else: raise TypeError("Unrecognized encoding mode")
        
        datalloader = DataLoader(dataset, shuffle=False, 
                                 batch_size=batch_size)
        pbar = tqdm(enumerate(datalloader), total=len(datalloader), 
                    desc=f"encoding {mode}", disable=not(use_tqdm))
        all_embeds = []
        for step, batch in pbar:
            with torch.no_grad():
                enc_args = (batch[0].to(device), batch[1].to(device))
                batch_embed = self.embed_model(*enc_args).pooler_output
                for embed in batch_embed: all_embeds.append(embed)
                # if step == 5: break # DEBUG
        # print(type(all_embeds[0]), len(all_embeds))
        return all_embeds
#     def write_encode_emb_libsvm(self, text_or_snippets: List[str], 
#                                 path: str, mode: str="text", **args):
#         """write the encoded embedding directly to a LIBSVM style text file."""
#         device_id = args.get("device_id", "cuda:0")
#         batch_size = args.get("batch_size", 32)
#         use_tqdm = args.get("use_tqdm", False)
        
#         device = device_id if torch.cuda.is_available() else "cpu"
#         self.to(device)
#         self.eval()
#         file_ptr = open(path, "w")
        
#         if mode == "text":
#             dataset = TextDataset(text_or_snippets, tokenizer=self.tokenizer,
#                                   truncation=True, padding="max_length",
#                                   max_length=100, add_special_tokens=True,
#                                   return_tensors="pt")
#         elif mode == "code":
#             dataset = CodeDataset(text_or_snippets, tokenizer=self.tokenizer,
#                                   truncation=True, padding="max_length",
#                                   max_length=100, add_special_tokens=True,
#                                   return_tensors="pt")
#         else: raise TypeError("Unrecognized encoding mode")
#         datalloader = DataLoader(dataset, shuffle=False, 
#                                  batch_size=batch_size)
#         pbar = tqdm(enumerate(datalloader), total=len(datalloader), 
#                     desc=f"enocding {mode}", disable=not(use_tqdm))
#         for step, batch in pbar:
#             with torch.no_grad():
#                 enc_args = (batch[0].to(device), batch[1].to(device))
#                 batch_embed = self.embed_model(*enc_args).pooler_output
#                 for embed in batch_embed:
#                     file_ptr.write(str(embed)+"\n")
                # if step == 5: break # DEBUG
    def fit(self, train_path: str, val_path: str, **args):
        warmup_steps = args.get("warmup_steps", 3000) # NEW
        exp_name = args.get("exp_name", "experiment")
        VALID_STEPS = args["valid_steps"]
        device_id = args.get("device_id", "cuda:0")
        batch_size = args.get("batch_size", 32)
        use_scl = self.use_scl
        use_ccl = self.use_ccl
        if use_scl: print("\x1b[34;1musing selectively contrastive loss\x1b[0m")
        if use_ccl: print("\x1b[34;1musing code contrastive loss\x1b[0m")
        epochs = args.get("epochs", 5)
        beta = args.get("beta", 0.01) # NEW
        p = args.get("p") # NEW
        do_dynamic_negative_sampling = args.get("dynamic_negative_sampling", False)
        use_curriculum = not(args.get("no_curriculum", False))
        curriculum_type = args.get("curriculum_type", "mr")
        
        use_AST = args.get("use_AST", False)
        sim_intents_path = args.get("sim_intents_path")
        code_code_pairs_path = args.get("code_code_pairs_path")
        perturbed_codes_path = args.get("perturbed_codes_path")
        intent_level_dynamic_sampling = args.get("intent_level_dynamic_sampling", False)
        
        device = device_id if torch.cuda.is_available() else "cpu"
        save_path = os.path.join(exp_name, "model.pt")
        # create experiment folder.
        os.makedirs(exp_name, exist_ok=True)
        # save params to config file.
        self.config["batch_size"] = batch_size
        self.config["train_path"] = train_path
        self.config["device_id"] = device_id
        self.config["exp_name"] = exp_name
        self.config["val_path"] = val_path
        self.config["epochs"] = epochs
        self.config["dynamic_negative_sampling"] = do_dynamic_negative_sampling
        self.config["use_AST"] = use_AST
        self.config["intent_level_dynamic_sampling"] = intent_level_dynamic_sampling
        
        print(f"model will be saved at {save_path}")
        print(f"moving model to {device}")
        self.embed_model.to(device)
        sim_intents_map = {}
        perturbed_codes = {}
        if intent_level_dynamic_sampling or use_AST:
            print("\x1b[33;1mdoing idns or AST training\x1b[0m")
            if intent_level_dynamic_sampling:
                assert sim_intents_path is not None, "Missing path to dictionary containing similar intents corresponding to an intent"
                sim_intents_map = json.load(open(sim_intents_path))
                perturbed_codes = {}
            if use_AST:
                assert perturbed_codes_path is not None, "Missing path to dictionary containing perturbed codes corresponding to a given code snippet"
                perturbed_codes = json.load(open(perturbed_codes_path))
            # create the data loaders.
            trainset = DynamicTriplesDataset(
                train_path, "codebert", device=device_id, beta=beta, warmup_steps=warmup_steps,
                sim_intents_map=sim_intents_map, perturbed_codes=perturbed_codes, use_AST=use_AST, model=self, 
                tokenizer=self.tokenizer, p=p, use_curriculum=use_curriculum, curriculum_type=curriculum_type,
                ignore_non_disco_rules=self.ignore_non_disco_rules, ignore_old_worst_rules=self.ignore_old_worst_rules,
                ignore_new_rules=self.ignore_new_rules, ignore_unnatural_rules=self.ignore_unnatural_rules,
                batch_size=batch_size, num_epochs=epochs, ignore_worst_rules=self.ignore_worst_rules,
                max_length=100, padding="max_length", return_tensors="pt", 
                add_special_tokens=True, truncation=True,
            )
            valset = ValRetDataset(val_path)
            # trainset = DynamicTriplesDataset(
            #     train_path, "codebert", sim_intents_map=sim_intents_map,
            #     perturbed_codes=perturbed_codes, use_AST=use_AST, model=self, 
            #     tokenizer=self.tokenizer, device=device_id, max_length=100, 
            #     padding="max_length", return_tensors="pt", 
            #     add_special_tokens=True, truncation=True,
            # )
            # valset = DynamicTriplesDataset(
            #     val_path, "codebert", model=self, val=True, 
            #     tokenizer=self.tokenizer, max_length=100, 
            #     padding="max_length", return_tensors="pt", 
            #     truncation=True, add_special_tokens=True,
            # )
            self.config["trainset.warmup_steps"] = trainset.warmup_steps # no. of warmup steps before commencing training.
            self.config["trainset.epsilon"] = trainset.epsilon # related to mastering rate.
            self.config["trainset.delta"] = trainset.soft_master_rate.delta # related to mastering rate.
            self.config["trainset.beta"] = trainset.beta # related to hard negative sampling.
            self.config["trainset.p"] = trainset.soft_master_rate.p # related to mastering rate.
        elif self.code_retriever_baseline:
            print("\x1b[33;1mdoing code_retriever_baseline training\x1b[0m")
            if self.code_retriever_triplets:
                trainset = CodeRetrieverTriplesDataset(
                    train_path, model_name="codebert", tokenizer=self.tokenizer,
                    max_length=100, padding="max_length", return_tensors="pt", 
                    add_special_tokens=True, truncation=True,
                )
            else:
                trainset = CodeRetrieverDataset(
                    train_path, code_code_path=code_code_pairs_path, model_name="codebert", tokenizer=self.tokenizer,
                    max_length=100, padding="max_length", return_tensors="pt", add_special_tokens=True, truncation=True,
                )
            # valset = ValRetDataset(val_path)
        else:
            print("\x1b[33;1mdoing regular training (TripletMarginLoss)\x1b[0m")
            global SHUFFLE_BATCH_DEBUG_SETTING
            SHUFFLE_BATCH_DEBUG_SETTING = False
            trainset = TriplesDataset(train_path, tokenizer=self.tokenizer,
                                      truncation=True, padding="max_length",
                                      max_length=100, add_special_tokens=True,
                                      return_tensors="pt")
            # valset = TriplesDataset(val_path, tokenizer=self.tokenizer,
            #                         truncation=True, padding="max_length",
            #                         max_length=100, add_special_tokens=True,
            #                         return_tensors="pt")
        valset = ValRetDataset(val_path)
        # save config file.
        config_path = os.path.join(exp_name, "config.json")
        with open(config_path, "w") as f:
            print(self.config)
            json.dump(self.config, f)
        print(f"saved config to {config_path}")
        if SHUFFLE_BATCH_DEBUG_SETTING and not(self.code_retriever_baseline): 
            #TODO: remove this. Used only for a temporary experiment.
            trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size,
                                     collate_fn=batch_shuffle_collate_fn_codebert)
            valloader = DataLoader(valset, shuffle=False, batch_size=batch_size,
                                   collate_fn=batch_shuffle_collate_fn_codebert)
        else:
            trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size)
            valloader = DataLoader(valset, shuffle=False, batch_size=batch_size)
        train_metrics = {
            "log_steps": [],
            "summary": [],
        } 
        rule_wise_acc = RuleWiseAccuracy(margin=1, use_scl=self.use_scl)
        if self.comb_exp:
            train_tot = 0; train_acc = 0
            train_hard_neg_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
        elif not(self.use_cross_entropy or self.code_retriever_baseline):
            train_soft_neg_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
            train_hard_neg_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
        else: train_tot = 0; train_acc = 0; train_u_acc = 0
        best_val_acc = 0
        for epoch_i in range(epochs):
            self.train()
            batch_losses = []
            soft_neg_weights = []
            pbar = tqdm(enumerate(trainloader), total=len(trainloader),
                        desc=f"train: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0 acc: 0")
            rule_wise_acc.reset()
            if self.comb_exp: train_tot = 0; train_acc = 0; train_hard_neg_acc.reset()
            elif not(self.use_cross_entropy or self.code_retriever_baseline):
                train_soft_neg_acc.reset(); train_hard_neg_acc.reset()
            for step, batch in pbar:
                if do_dynamic_negative_sampling:
                    batch = dynamic_negative_sampling(
                        self.embed_model, batch, 
                        model_name="codebert", 
                        device=device, k=1,
                    )
                self.train()
                anchor_title = (batch[0].to(device), batch[1].to(device))
                pos_snippet = (batch[2].to(device), batch[3].to(device))
                neg_snippet = (batch[4].to(device), batch[5].to(device))
                if self.code_retriever_skip_unimodal and not(self.code_retriever_triplets):
                    anchor_text_emb = self.embed_model(*anchor_title).pooler_output
                    pos_code_emb = self.embed_model(*pos_snippet).pooler_output
                else:
                    anchor_text_emb, pos_code_emb, neg_code_emb = self(
                        anchor_title, pos_snippet, neg_snippet
                    )
                N = len(batch[0])
                if hasattr(trainset, "update") or isinstance(trainset, (CodeRetrieverDataset, CodeRetrieverTriplesDataset)):
                    if self.comb_exp:
                        train_hard_neg_acc.update(
                            anchor_text_emb, pos_code_emb, 
                            neg_code_emb, (batch[-1]!=0).cpu(),
                        )
                        HARD_ACC = f" ha:{100*train_hard_neg_acc.get():.2f}"
                    elif not(self.use_cross_entropy or self.code_retriever_baseline):
                        train_soft_neg_acc.update(
                            anchor_text_emb, pos_code_emb, 
                            neg_code_emb, (batch[-1]==0).cpu(),
                        )
                        train_hard_neg_acc.update(
                            anchor_text_emb, pos_code_emb, 
                            neg_code_emb, (batch[-1]!=0).cpu(),
                        )
                        trainset.update(
                            train_soft_neg_acc.last_batch_acc,
                            train_hard_neg_acc.last_batch_acc,
                        )
                        HARD_ACC = f" ha:{100*train_hard_neg_acc.get():.2f}"
                        MIX_STEP = trainset.mix_step()
                    if use_scl:
                        batch_loss = scl_loss(
                            anchor_text_emb, pos_code_emb, 
                            neg_code_emb, lamb=1, device=device,
                            loss_fn=self.loss_fn,
                        ).mean()
                        pd_ap = F.pairwise_distance(anchor_text_emb, pos_code_emb).mean().item()
                        pd_an = F.pairwise_distance(anchor_text_emb, neg_code_emb).mean().item()
                        pd_ap_an_info = f" ap:{pd_ap:.3f} an:{pd_an:.3f}"
                        # hard_loss = self.loss_fn(anchor_text_emb, torch.zeros_like(
                        #                          pos_code_emb), neg_code_emb)
                        # soft_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb)
                        # batch[-1] = batch[-1].to(device)
                        # batch_loss = (batch[-1]*hard_loss + (~batch[-1])*soft_loss).mean()
                    elif self.use_cross_entropy:
                        d_ap = torch.cdist(anchor_text_emb, pos_code_emb)
                        d_an = torch.cdist(anchor_text_emb, neg_code_emb)
                        scores = -torch.cat((d_ap, d_an), axis=-1)
                        target = torch.as_tensor(range(N)).to(device)
                        batch_loss = self.ce_loss(scores, target)
                        preds = scores.argmax(dim=-1)
                        train_acc += (preds == target).sum().item()
                        train_tot += N
                        batch_loss_str = f"bl:{batch_loss:.3f}"
                        metric_str = f"a:{(100*train_acc/train_tot):.2f}"
                    elif self.comb_exp: # assuming hard curriculum.
                        d_ap = torch.cdist(anchor_text_emb, pos_code_emb)
                        d_an = torch.cdist(anchor_text_emb, neg_code_emb)
                        scores = -torch.cat((d_ap, d_an), axis=-1)
                        target = torch.as_tensor(range(N)).to(device)
                        soft_hard_ce_loss = self.ce_loss(scores, target) # CE loss
                        soft_ml_loss = self.loss_fn(anchor_text_emb, pos_code_emb, 
                                                    pos_code_emb[torch.randperm(N)]).mean() # margin based loss
                        batch_loss = soft_hard_ce_loss + soft_ml_loss
                        preds = scores.argmax(dim=-1)
                        train_acc += (preds == target).sum().item()
                        train_tot += N
                        batch_loss_str = f"bl:{batch_loss:.3f}={soft_hard_ce_loss}ce+{soft_ml_loss}ml"
                        metric_str = f"a:{(100*train_acc/train_tot):.2f}" # soft neg accuracy.
                    elif self.code_retriever_baseline:
                        if self.code_retriever_ml_loss:
                            if not(self.code_retriever_skip_unimodal):
                                unimodal_loss = self.loss_fn(anchor_text_emb, pos_code_emb, 
                                                             pos_code_emb[torch.randperm(N)]).mean()
                            bimodal_loss = self.loss_fn(pos_code_emb, neg_code_emb, 
                                                        neg_code_emb[torch.randperm(N)]).mean()
                        elif self.code_retriever_triplets:
                            # if USE_DROPOUT_NEGS:
                            #     d_ap = torch.cat((torch.cdist(
                            #                       self.dropout1(anchor_text_emb), 
                            #                       self.dropout2(pos_code_emb)), 
                            #                       torch.cdist(
                            #                       self.dropout1(anchor_text_emb), 
                            #                       self.dropout2(neg_code_emb))), axis=-1)
                                # d_ap = torch.cat((torch.cdist(anchor_text_emb, pos_code_emb), 
                                #                   torch.cdist(anchor_text_emb, neg_code_emb), 
                                #                   torch.cdist(anchor_text_emb, self.dropout1(
                                #                   neg_code_emb))), axis=-1)
                            # else:
                            d_ap = torch.cat((torch.cdist(anchor_text_emb, pos_code_emb), 
                                              torch.cdist(anchor_text_emb, neg_code_emb)), 
                                              axis=-1)
                            target = torch.as_tensor(range(N)).to(device)
                            bimodal_loss = self.ce_loss(-d_ap, target)
                            batch_loss = bimodal_loss
                        else:
                            if self.use_csim:
                                d_ap = -cos_csim(anchor_text_emb, pos_code_emb)
                                if not(self.code_retriever_skip_unimodal):
                                    d_pn = -cos_csim(pos_code_emb, neg_code_emb)
                            else:
                                d_ap = torch.cdist(anchor_text_emb, pos_code_emb)
                                if not(self.code_retriever_skip_unimodal):
                                    d_pn = torch.cdist(pos_code_emb, neg_code_emb)
                            # margin = self.config['margin']*torch.eye(N).to(device)
                            target = torch.as_tensor(range(N)).to(device)
                            if not(self.code_retriever_skip_unimodal):
                                unimodal_loss = self.ce_loss(-d_pn, target)
                            bimodal_loss = self.ce_loss(-d_ap, target)
                        # unimodal_loss = self.ce_loss(-(d_ap+margin), target)
                        # bimodal_loss = self.ce_loss(-(d_pn+margin), target)
                        if self.code_retriever_skip_unimodal:
                            batch_loss = bimodal_loss
                        else: batch_loss = unimodal_loss + bimodal_loss
                        if self.code_retriever_ml_loss: metric_str = ""
                        else:
                            b_preds = (-d_ap).argmax(dim=-1)
                            if not(self.code_retriever_skip_unimodal):
                                u_preds = (-d_pn).argmax(dim=-1)
                                train_u_acc += (u_preds == target).sum().item()
                            train_acc += (b_preds == target).sum().item()
                            train_tot += N
                        if self.code_retriever_skip_unimodal:
                            metric_str = f"ba:{(100*train_acc/train_tot):.2f}"
                            batch_loss_str = f"bl:{bimodal_loss:.3f}"
                        else: 
                            metric_str = f"ba:{(100*train_acc/train_tot):.2f} ua:{(100*train_u_acc/train_tot):.2f}"
                            batch_loss_str = f"bl:{batch_loss:.3f}={unimodal_loss:.3f}u+{bimodal_loss:.3f}b"
                    else:
                        # pd_ap = F.pairwise_distance(anchor_text_emb, pos_code_emb)
                        # pd_an = F.pairwise_distance(anchor_text_emb, neg_code_emb)
                        # hard_neg_ctr = (pd_ap > pd_an).sum().item()
                        # pd_ap_an_info = f" ap:{pd_ap.mean().item():.3f} an:{pd_an.mean().item():.3f} {hard_neg_ctr}/{batch_size}"
                        if self.use_ccl: # use code contrastive loss (by default all negatives are hard negatives)
                            """the self distance (diagonal terms) in d_pp will always be zero
                            the cross distance is always positive so a code is always more similar to itself
                            than other codes. To overcome this we can add a margin term (a diagonal matrix) 
                            to d_pp to make sure the pos_code_emb has at least distance equal to this margin
                            compared to any other negative. Here we take this margin to be the same as the 
                            margin for the triplet margin loss."""
                            # margin = self.config["margin"]*torch.eye(N).to(device)
                            S_pp = cos_csim(self.dropout1(pos_code_emb), self.dropout2(pos_code_emb))
                            S_pn = cos_csim(self.dropout1(pos_code_emb), neg_code_emb)
                            # scores = -torch.cat((d_pp+margin, d_pn), axis=-1)
                            scores = torch.cat((S_pp, S_pn), axis=-1)
                            target = torch.as_tensor(range(N)).to(device)
                            soft_margin_loss = self.loss_fn(anchor_text_emb, pos_code_emb, 
                                                            pos_code_emb[torch.randperm(N)]).mean()
                            # hard_margin_loss = self.loss_fn(anchor_text_emb, pos_code_emb, 
                            #                                 neg_code_emb).mean()
                            ccl_loss = self.ce_loss(scores, target)
                            batch_loss = soft_margin_loss + ccl_loss
                            batch_loss_str = f"bl:{batch_loss:.3f}={soft_margin_loss:.3f}+{ccl_loss:.3f}"
                            # batch_loss = soft_margin_loss + hard_margin_loss + ccl_loss
                            # batch_loss_str = f"bl:{batch_loss:.3f}={soft_margin_loss:.3f}+{hard_margin_loss:.3f}+{ccl_loss:.3f}"
                        else: 
                            batch_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb).mean()
                            batch_loss_str = f"bl:{batch_loss:.3f}"
                    if not(self.code_retriever_skip_unimodal):
                        rule_wise_acc.update(anchor_text_emb, pos_code_emb, 
                                             neg_code_emb, batch[-1].cpu().tolist())
                    if (self.use_cross_entropy or self.code_retriever_baseline):
                        pbar.set_description(f"T e:{epoch_i+1}/{epochs} bl:{batch_loss:.3f} l:{np.mean(batch_losses):.3f} {metric_str}")
                    elif self.comb_exp:
                        pbar.set_description(f"T e:{epoch_i+1}/{epochs} bl:{batch_loss:.3f} l:{np.mean(batch_losses):.3f} {metric_str}{HARD_ACC}")
                    else:
                        soft_neg_weight = 1 if trainset.warmup_steps >=0 else trainset.soft_neg_weight
                        soft_neg_weights.append(trainset.soft_neg_weight)
                        pbar.set_description(
                            f"T e:{epoch_i+1}/{epochs} {MIX_STEP}{batch_loss_str} l:{np.mean(batch_losses):.3f} a:{100*train_soft_neg_acc.get():.2f}{HARD_ACC}"
                        )
                else: 
                    train_soft_neg_acc.update(
                        anchor_text_emb, 
                        pos_code_emb, neg_code_emb
                    )
                    batch_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb).mean()
                    batch_loss_str = f"bl:{batch_loss:.3f}"
                    pbar.set_description(f"T e:{epoch_i+1}/{epochs} {batch_loss_str} l:{np.mean(batch_losses):.3f} a:{100*train_soft_neg_acc.get():.2f}")
                batch_loss.backward()
                self.optimizer.step() 
                self.zero_grad()
                batch_losses.append(batch_loss.item())
                
                # pbar.set_description(f"train: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.3f} loss: {np.mean(batch_losses):.3f} acc: {100*train_acc.get():.2f}")
                # if step == 5: break # DEBUG
                if ((step+1) % VALID_STEPS == 0) or ((step+1) == len(trainloader)):
                    # validate current model
                    print(rule_wise_acc())
                    print(dict(rule_wise_acc.counts))
                    # if intent_level_dynamic_sampling or use_AST or self.code_retriever_baseline:
                    s = time.time()
                    val_acc = self.val_ret(valset, device=device)
                    print(f"validated in {time.time()-s}s")
                    print(f"recall@5 = {100*val_acc:.3f}")
                    val_loss = None
                    # else:
                    #     val_acc, val_loss = self.val(valloader, epoch_i=epoch_i, 
                    #                                  epochs=epochs, device=device)
                    # save model only after warmup is complete (for MR curriculum).
                    if val_acc > best_val_acc and (not(hasattr(trainset, "warmup_steps")) or trainset.warmup_steps == 0):
                        best_val_acc = val_acc
                        print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
                        torch.save(self.state_dict(), save_path)

                    train_metrics["log_steps"].append({
                        "soft_neg_weights": soft_neg_weights,
                        "train_batch_losses": batch_losses, 
                        "train_loss": np.mean(batch_losses), 
                        "val_loss": val_loss,
                        "val_acc": 100*val_acc,
                    })
                    if (self.use_cross_entropy or self.code_retriever_baseline and not(self.code_retriever_ml_loss)):
                        train_metrics["train_acc"] = 100*train_acc/train_tot
                        if self.code_retriever_baseline and not(self.code_retriever_ml_loss) and not(self.code_retriever_skip_unimodal):
                            train_metrics["train_u_acc"] = 100*train_u_acc/train_tot
                    elif self.comb_exp:
                        train_metrics["train_acc"] = 100*train_acc/train_tot
                        train_metrics["train_hard_neg_acc"] = 100*train_hard_neg_acc.get()
                    elif not(self.code_retriever_ml_loss):
                        train_metrics["train_soft_neg_acc"] = 100*train_soft_neg_acc.get()
                        train_metrics["train_hard_neg_acc"] = 100*train_hard_neg_acc.get()
                    metrics_path = os.path.join(exp_name, "train_metrics.json")
                    print(f"saving metrics to {metrics_path}")
                    with open(metrics_path, "w") as f:
                        json.dump(train_metrics, f)
#             val_acc, val_loss = self.val(valloader, epoch_i=epoch_i, 
#                                          epochs=epochs, device=device)
#             if val_acc > best_val_acc:
#                 print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
#                 best_val_acc = val_acc
#                 torch.save(self.state_dict(), save_path)
            if self.code_retriever_baseline: trainset.reset()
#             train_metrics["epochs"].append({
#                 "train_batch_losses": batch_losses, 
#                 "train_loss": np.mean(batch_losses), 
#                 "train_acc": 100*train_acc.get(),
#                 "val_loss": val_loss,
#                 "val_acc": 100*val_acc,
#             })
        return train_metrics

    # def fit_code_retriever_quint(self, train_path: str, val_path: str, **args):
    #     """fit the model on data having hard negatives using CodeRetriever like bimodal losses"""
    #     exp_name = args.get("exp_name", "experiment")
    #     device_id = args.get("device_id", "cuda:0")
    #     batch_size = args.get("batch_size", 32)
    #     epochs = args.get("epochs", 5)
    #     device = device_id if torch.cuda.is_available() else "cpu"
    #     save_path = os.path.join(exp_name, "model.pt")
    #     os.makedirs(exp_name, exist_ok=True) # create experiment folder.
    #     # save params to config file.
    #     self.config["batch_size"] = batch_size
    #     self.config["train_path"] = train_path
    #     self.config["device_id"] = device_id
    #     self.config["exp_name"] = exp_name
    #     self.config["val_path"] = val_path
    #     self.config["epochs"] = epochs
    #     print(f"model will be saved at {save_path}")
    #     print(f"moving model to {device}")
    #     self.embed_model.to(device)
    #     # create the datasets and data loaders.
    #     trainset = CodeRetrieverQuintsDataset(
    #         train_path, model_name="codebert", tokenizer=self.tokenizer, 
    #         max_length=100, padding="max_length", return_tensors="pt", 
    #         add_special_tokens=True, truncation=True,
    #     )
    #     valset = ValRetDataset(val_path)
    #     config_path = os.path.join(exp_name, "config.json")
    #     with open(config_path, "w") as f: 
    #         print(self.config)
    #         json.dump(self.config, f) # save config file.
    #     print(f"saved config to {config_path}")
    #     trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size)
    #     train_metrics = {"log_steps": [], "summary": []} 
    #     # bimodal, soft cross entropy loss (classification accuracy)
    #     train_tot = 0
    #     train_acc = 0
    #     best_val_acc = 0
    #     for epoch_i in range(epochs):
    #         self.train()
    #         batch_losses = []
    #         pbar = tqdm(enumerate(trainloader), total=len(trainloader),
    #                     desc=f"train: epoch: {epoch_i+1}/{epochs}")
    #         for step, batch in pbar:
    #             self.train()
    #             vecs = []
    #             for i in range(0, 10, 2):
    #                 input_ids = batch[i]
    #                 attn_mask = batch[i+1]
    #                 vecs.append(self.embed_model(
    #                     input_ids.to(device), 
    #                     attn_mask.to(device),
    #                 ).pooler_output)
    #             # vecs: 0: a, 1: p, 2: n1, 3: n2
    #             N = len(batch[0])
    #             d_ap = torch.cdist(vecs[0], vecs[1])
    #             d_an1 = torch.cdist(vecs[0], vecs[2])
    #             d_an2 = torch.cdist(vecs[0], vecs[3]) 
    #             d_an3 = torch.cdist(vecs[0], vecs[4])
    #             scores = -torch.cat((d_ap, d_an1, d_an2, d_an3), axis=-1)
    #             target = torch.as_tensor(range(N)).to(device)
    #             batch_loss = self.ce_loss(scores, target) # CE bimodal loss.
    #             batch_loss.backward() # compute gradients.
    #             self.optimizer.step() # take optimization step.
    #             self.zero_grad() # clear gradients
    #             batch_losses.append(batch_loss.item()) # collect batch losses.
    #             train_tot += N # update metrics.
    #             preds = scores.argmax(dim=-1)
    #             train_acc += (preds == target).sum().item()
    #             batch_loss_str = f"bl:{batch_loss:.2f}" # batch loss string (show values of various losses)
    #             metric_str = f"a:{100*train_acc/train_tot:.3f}" # show metrics
    #             pbar.set_description(f"T e:{epoch_i+1}/{epochs} {batch_loss_str} l:{np.mean(batch_losses):.3f} {metric_str}")
    #             if ((step+1) % VALID_STEPS == 0) or ((step+1) == len(trainloader)):
    #                 # validate current model
    #                 s = time.time()
    #                 val_acc = self.val_ret(valset, device=device)
    #                 print(f"validated in {time.time()-s}s")
    #                 print(f"recall@5 = {100*val_acc:.3f}")
    #                 val_loss = None
    #                 # save model only after warmup is complete (for MR curriculum).
    #                 if val_acc > best_val_acc and (not(hasattr(trainset, "warmup_steps")) or trainset.warmup_steps == 0):
    #                     best_val_acc = val_acc
    #                     print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
    #                     torch.save(self.state_dict(), save_path)
    #                 train_metrics["log_steps"].append({
    #                     "train_batch_losses": batch_losses, 
    #                     "train_loss": np.mean(batch_losses), 
    #                     "val_loss": val_loss,
    #                     "val_acc": 100*val_acc,
    #                 })
    #                 train_metrics["train_acc"] = 100*train_acc/train_tot
    #                 metrics_path = os.path.join(exp_name, "train_metrics.json")
    #                 print(f"saving metrics to {metrics_path}")
    #                 with open(metrics_path, "w") as f:
    #                     json.dump(train_metrics, f)

    #     return train_metrics
    
    # def fit_code_retriever_quad(self, train_path: str, val_path: str, **args):
    #     """fit the model on data having hard negatives using CodeRetriever like bimodal losses"""
    #     exp_name = args.get("exp_name", "experiment")
    #     device_id = args.get("device_id", "cuda:0")
    #     batch_size = args.get("batch_size", 32)
    #     epochs = args.get("epochs", 5)
    #     device = device_id if torch.cuda.is_available() else "cpu"
    #     save_path = os.path.join(exp_name, "model.pt")
    #     os.makedirs(exp_name, exist_ok=True) # create experiment folder.
    #     # save params to config file.
    #     self.config["batch_size"] = batch_size
    #     self.config["train_path"] = train_path
    #     self.config["device_id"] = device_id
    #     self.config["exp_name"] = exp_name
    #     self.config["val_path"] = val_path
    #     self.config["epochs"] = epochs
    #     print(f"model will be saved at {save_path}")
    #     print(f"moving model to {device}")
    #     self.embed_model.to(device)
    #     # create the datasets and data loaders.
    #     trainset = CodeRetrieverQuadsDataset(
    #         train_path, model_name="codebert", tokenizer=self.tokenizer, 
    #         max_length=100, padding="max_length", return_tensors="pt", 
    #         add_special_tokens=True, truncation=True,
    #     )
    #     valset = ValRetDataset(val_path)
    #     config_path = os.path.join(exp_name, "config.json")
    #     with open(config_path, "w") as f: 
    #         print(self.config)
    #         json.dump(self.config, f) # save config file.
    #     print(f"saved config to {config_path}")
    #     trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size)
    #     train_metrics = {"log_steps": [], "summary": []} 
    #     # bimodal, soft cross entropy loss (classification accuracy)
    #     train_tot = 0
    #     train_acc = 0
    #     best_val_acc = 0
    #     for epoch_i in range(epochs):
    #         self.train()
    #         batch_losses = []
    #         pbar = tqdm(enumerate(trainloader), total=len(trainloader),
    #                     desc=f"train: epoch: {epoch_i+1}/{epochs}")
    #         for step, batch in pbar:
    #             self.train()
    #             vecs = []
    #             for i in range(0, 8, 2):
    #                 input_ids = batch[i]
    #                 attn_mask = batch[i+1]
    #                 vecs.append(self.embed_model(
    #                     input_ids.to(device), 
    #                     attn_mask.to(device),
    #                 ).pooler_output)
    #             # vecs: 0: a, 1: p, 2: n1, 3: n2
    #             N = len(batch[0])
    #             d_ap = torch.cdist(vecs[0], vecs[1])
    #             d_an1 = torch.cdist(vecs[0], vecs[2])
    #             d_an2 = torch.cdist(vecs[0], vecs[3]) 
    #             scores = -torch.cat((d_ap, d_an1, d_an2), axis=-1)
    #             target = torch.as_tensor(range(N)).to(device)
    #             batch_loss = self.ce_loss(scores, target) # CE bimodal loss.
    #             batch_loss.backward() # compute gradients.
    #             self.optimizer.step() # take optimization step.
    #             self.zero_grad() # clear gradients
    #             batch_losses.append(batch_loss.item()) # collect batch losses.
    #             train_tot += N # update metrics.
    #             preds = scores.argmax(dim=-1)
    #             train_acc += (preds == target).sum().item()
    #             batch_loss_str = f"bl:{batch_loss:.2f}" # batch loss string (show values of various losses)
    #             metric_str = f"a:{100*train_acc/train_tot:.3f}" # show metrics
    #             pbar.set_description(f"T e:{epoch_i+1}/{epochs} {batch_loss_str} l:{np.mean(batch_losses):.3f} {metric_str}")
    #             if ((step+1) % VALID_STEPS == 0) or ((step+1) == len(trainloader)):
    #                 # validate current model
    #                 s = time.time()
    #                 val_acc = self.val_ret(valset, device=device)
    #                 print(f"validated in {time.time()-s}s")
    #                 print(f"recall@5 = {100*val_acc:.3f}")
    #                 val_loss = None
    #                 # save model only after warmup is complete (for MR curriculum).
    #                 if val_acc > best_val_acc and (not(hasattr(trainset, "warmup_steps")) or trainset.warmup_steps == 0):
    #                     best_val_acc = val_acc
    #                     print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
    #                     torch.save(self.state_dict(), save_path)
    #                 train_metrics["log_steps"].append({
    #                     "train_batch_losses": batch_losses, 
    #                     "train_loss": np.mean(batch_losses), 
    #                     "val_loss": val_loss,
    #                     "val_acc": 100*val_acc,
    #                 })
    #                 train_metrics["train_acc"] = 100*train_acc/train_tot
    #                 metrics_path = os.path.join(exp_name, "train_metrics.json")
    #                 print(f"saving metrics to {metrics_path}")
    #                 with open(metrics_path, "w") as f:
    #                     json.dump(train_metrics, f)

    #     return train_metrics

    # def fit_unibi_hardneg(self, train_path: str, val_path: str, **args):
    #     """fit the model on data having hard negatives using unimodal+bimodal losses"""
    #     exp_name = args.get("exp_name", "experiment")
    #     device_id = args.get("device_id", "cuda:0")
    #     batch_size = args.get("batch_size", 32)
    #     epochs = args.get("epochs", 5)
    #     # use_curriculum = not(args.get("no_curriculum", False))
    #     # curriculum_type = args.get("curriculum_type", "mr")
    #     # warmup_steps = args.get("warmup_steps", 3000)
    #     # beta = args.get("beta", 0.01)
    #     # p = args.get("p")
    #     code_syns_path = args.get("code_syns_path")
    #     sim_intents_path = args.get("sim_intents_path")
    #     perturbed_codes_path = args.get("perturbed_codes_path")
    #     device = device_id if torch.cuda.is_available() else "cpu"
    #     save_path = os.path.join(exp_name, "model.pt")
    #     os.makedirs(exp_name, exist_ok=True) # create experiment folder.
    #     # save params to config file.
    #     self.config["uni_bi_hard_neg"] = True
    #     self.config["batch_size"] = batch_size
    #     self.config["train_path"] = train_path
    #     self.config["device_id"] = device_id
    #     self.config["exp_name"] = exp_name
    #     self.config["val_path"] = val_path
    #     self.config["epochs"] = epochs
    #     print(f"model will be saved at {save_path}")
    #     print(f"moving model to {device}")
    #     self.embed_model.to(device)
    #     sim_intents_map = {}
    #     perturbed_codes = {}
    #     # msg = "Missing path to dictionary containing {} corresponding to a given {}"
    #     # assert sim_intents_path is not None, msg.format("similar intents", "intent")
    #     if sim_intents_path is not None:
    #         sim_intents_map = json.load(open(sim_intents_path))
    #     # assert perturbed_codes_path is not None, msg.format("perturbed codes", "code snippet")
    #     if perturbed_codes_path is not None:
    #         perturbed_codes = json.load(open(perturbed_codes_path))
    #     # create the datasets and data loaders.
    #     trainset = UniBiHardNegDataset(
    #         train_path, code_syns_path=code_syns_path, model_name="codebert", model=self,
    #         device=device_id, sim_intents_map=sim_intents_map, perturbed_codes=perturbed_codes, 
    #         batch_size=64, tokenizer=self.tokenizer, max_length=100, padding="max_length", 
    #         return_tensors="pt", add_special_tokens=True, truncation=True,
    #         # ignore_worst_rules=self.ignore_worst_rules
    #         # ignore_non_disco_rules=self.ignore_non_disco_rules,
    #     )
    #     valset = ValRetDataset(val_path)
    #     # self.config["trainset.warmup_steps"] = trainset.warmup_steps # no. of warmup steps before commencing training.
    #     # self.config["trainset.epsilon"] = trainset.epsilon # related to mastering rate.
    #     # self.config["trainset.delta"] = trainset.soft_master_rate.delta # related to mastering rate.
    #     # self.config["trainset.beta"] = trainset.beta # related to hard negative sampling.
    #     # self.config["trainset.p"] = trainset.soft_master_rate.p # related to mastering rate.
    #     config_path = os.path.join(exp_name, "config.json") # path to config file
    #     with open(config_path, "w") as f: print(self.config); json.dump(self.config, f) # save config file.
    #     print(f"saved config to {config_path}")
    #     trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size)
    #     train_metrics = {"log_steps": [], "summary": []} 
    #     rule_wise_acc = RuleWiseAccuracy(margin=1, use_scl=self.use_scl)
    #     # bimodal, (soft+hard) cross entropy loss (classification accuracy)
    #     train_ce_bi_tot = 0
    #     train_ce_bi_acc = 0
    #     # unimodal, (soft+hard) cross entropy loss (classification accuracy)
    #     train_ce_uni_tot = 0 
    #     train_ce_uni_acc = 0
    #     # triplet accuracies
    #     train_ml_bi_soft_acc = TripletAccuracy(margin=1) # bimodal, soft negatives, margin loss (triplet accuracy)
    #     train_ml_bi_hard_acc = TripletAccuracy(margin=1) # bimodal, hard negatives, margin loss (triplet accuracy)
    #     train_ml_uni_soft_acc = TripletAccuracy(margin=1) # unimodal, soft negatives, margin loss (triplet accuracy)
    #     train_ml_uni_hard_acc = TripletAccuracy(margin=1) # unimodal, hard negatives, margin loss (triplet accuracy)
    #     best_val_acc = 0
    #     for epoch_i in range(epochs):
    #         self.train()
    #         batch_losses = []
    #         pbar = tqdm(enumerate(trainloader), total=len(trainloader),
    #                     desc=f"train: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0 acc: 0")
    #         # reset triplet accuracies.
    #         rule_wise_acc.reset()
    #         train_ml_bi_soft_acc.reset()
    #         train_ml_bi_hard_acc.reset()
    #         train_ml_uni_soft_acc.reset()
    #         train_ml_uni_hard_acc.reset()
    #         for step, batch in pbar:
    #             self.train()
    #             vecs = []
    #             for i in range(0, 8, 2):
    #                 input_ids = batch[i]
    #                 attn_mask = batch[i+1]
    #                 vecs.append(self.embed_model(
    #                     input_ids.to(device), 
    #                     attn_mask.to(device),
    #                 ).pooler_output)
    #             # vecs: 0: a, 1: p, 2: p_, 3: n
    #             N = len(batch[0])
    #             pidx = torch.randperm(N) # permuted ids
    #             d_ap = torch.cdist(vecs[0], vecs[1])
    #             d_an = torch.cdist(vecs[0], vecs[3])
    #             d_pp_ = torch.cdist(vecs[1], vecs[2]) 
    #             d_pn = torch.cdist(vecs[1], vecs[3])
    #             b_scores = -torch.cat((d_ap, d_an), axis=-1)
    #             u_scores = -torch.cat((d_pp_, d_pn), axis=-1)
    #             target = torch.as_tensor(range(N)).to(device)
    #             # all 6 loss functions:
    #             ce_b_loss = self.ce_loss(b_scores, target) # CE bimodal loss.
    #             ce_u_loss = self.ce_loss(u_scores, target) # CE unimodal loss.
    #             # ml_b_hard_loss = self.loss_fn(vecs[0], vecs[1], vecs[3]).mean()
    #             # ml_u_hard_loss = self.loss_fn(vecs[1], vecs[2], vecs[3]).mean()
    #             ml_b_soft_loss = self.loss_fn(vecs[0], vecs[1], vecs[1][pidx]).mean()
    #             ml_u_soft_loss = self.loss_fn(vecs[1], vecs[2], vecs[2][pidx]).mean()
    #             # batch_loss = ce_b_loss + ce_u_loss + ml_b_hard_loss + ml_u_hard_loss + ml_b_soft_loss + ml_u_soft_loss # total batch loss.
    #             batch_loss = ce_b_loss + ce_u_loss + ml_b_soft_loss + ml_u_soft_loss
    #             batch_loss.backward() # compute gradients.
    #             self.optimizer.step() # take optimization step.
    #             self.zero_grad() # clear gradients
    #             batch_losses.append(batch_loss.item()) # collect batch losses.
    #             # update metrics.
    #             train_ce_bi_tot += N
    #             train_ce_uni_tot += N
    #             b_preds = b_scores.argmax(dim=-1)
    #             u_preds = u_scores.argmax(dim=-1)
    #             train_ce_bi_acc += (b_preds == target).sum().item()
    #             train_ce_uni_acc += (u_preds == target).sum().item()
    #             train_ml_bi_soft_acc.update(vecs[0], vecs[1], vecs[3][pidx])
    #             train_ml_bi_hard_acc.update(vecs[0], vecs[1], vecs[3], (batch[-1]!=0))
    #             train_ml_uni_soft_acc.update(vecs[1], vecs[2], vecs[3][pidx])
    #             train_ml_uni_hard_acc.update(vecs[1], vecs[2], vecs[3], (batch[-1]!=0))
    #             rule_wise_acc.update(vecs[0], vecs[1], vecs[3], batch[-1].tolist())
    #             # batch loss string (show values of various losses)
    #             # batch_loss_str = f"bl:{batch_loss:.2f}=ce:{ce_b_loss:.2f}b+{ce_u_loss:.2f}u|ml:{ml_b_hard_loss:.2f}bh+{ml_u_hard_loss:.2f}uh+{ml_b_soft_loss:.2f}bs+{ml_u_soft_loss:.2f}us"
    #             batch_loss_str = f"bl:{batch_loss:.2f}=ce:{ce_b_loss:.2f}b+{ce_u_loss:.2f}u|ml:{ml_b_soft_loss:.2f}bs+{ml_u_soft_loss:.2f}us"
    #             # show metrics
    #             ce_metric_str = f"{(100*train_ce_bi_acc/train_ce_bi_tot):.2f}b,{(100*train_ce_uni_acc/train_ce_uni_tot):.2f}u"
    #             ml_metric_str = f"{100*train_ml_bi_hard_acc.get():.2f}bh,{100*train_ml_uni_hard_acc.get():.2f}uh,{100*train_ml_bi_soft_acc.get():.2f}bs,{100*train_ml_uni_soft_acc.get():.2f}us"
    #             metric_str = f"ce:{ce_metric_str}|ml:{ml_metric_str}"
    #             pbar.set_description(f"T e:{epoch_i+1}/{epochs} {batch_loss_str} l:{np.mean(batch_losses):.3f} {metric_str}")
    #             # if step == 5: break # DEBUG
    #             if ((step+1) % VALID_STEPS == 0) or ((step+1) == len(trainloader)):
    #                 # validate current model
    #                 print(rule_wise_acc())
    #                 print(dict(rule_wise_acc.counts))
    #                 s = time.time()
    #                 val_acc = self.val_ret(valset, device=device)
    #                 print(f"validated in {time.time()-s}s")
    #                 print(f"recall@5 = {100*val_acc:.3f}")
    #                 val_loss = None
    #                 # save model only after warmup is complete (for MR curriculum).
    #                 if val_acc > best_val_acc and (not(hasattr(trainset, "warmup_steps")) or trainset.warmup_steps == 0):
    #                     best_val_acc = val_acc
    #                     print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
    #                     torch.save(self.state_dict(), save_path)
    #                 train_metrics["log_steps"].append({
    #                     "train_batch_losses": batch_losses, 
    #                     "train_loss": np.mean(batch_losses), 
    #                     "val_loss": val_loss,
    #                     "val_acc": 100*val_acc,
    #                 })
    #                 train_metrics["train_ce_bi_acc"] = 100*train_ce_bi_acc/train_ce_bi_tot
    #                 train_metrics["train_ce_uni_acc"] = 100*train_ce_uni_acc/train_ce_uni_tot
    #                 train_metrics["train_ml_bi_soft_acc"] = 100*train_ml_bi_soft_acc.get()
    #                 train_metrics["train_ml_bi_hard_acc"] = 100*train_ml_bi_hard_acc.get()
    #                 train_metrics["train_ml_uni_soft_acc"] = 100*train_ml_uni_soft_acc.get()
    #                 train_metrics["train_ml_uni_soft_acc"] = 100*train_ml_uni_hard_acc.get()
    #                 metrics_path = os.path.join(exp_name, "train_metrics.json")
    #                 print(f"saving metrics to {metrics_path}")
    #                 with open(metrics_path, "w") as f:
    #                     json.dump(train_metrics, f)

    #     return train_metrics
    
def main(args):
    print("initializing model and tokenizer ..")
    # tok_path = os.path.join(os.path.expanduser("~"), "codebert-base-tok")
    tok_path = get_tok_path("codebert")
    print("creating model object")
    triplet_net = CodeBERTripletNet(tok_path=tok_path, **vars(args))
    print("commencing training")
    if args.all_losses_setting: 
        metrics = triplet_net.fit_unibi_hardneg(**vars(args))
    elif args.disco_baseline:
        metrics = fit_disco(triplet_net, model_name="codebert", **vars(args))
    elif args.code_retriever_quad:
        metrics = triplet_net.fit_code_retriever_quad(**vars(args))
    elif args.code_retriever_quint:
        metrics = triplet_net.fit_code_retriever_quint(**vars(args))
    else:
        metrics = triplet_net.fit(exp_name=args.exp_name, epochs=args.epochs,
                                  perturbed_codes_path=args.perturbed_codes_path,
                                  device_id=args.device_id, val_path=args.val_path,
                                  train_path=args.train_path, batch_size=args.batch_size,
                                  beta=args.beta, p=args.p, warmup_steps=args.warmup_steps,
                                  dynamic_negative_sampling=args.dynamic_negative_sampling,
                                  sim_intents_path=args.sim_intents_path, use_AST=args.use_AST,
                                  intent_level_dynamic_sampling=args.intent_level_dynamic_sampling,
                                  no_curriculum=args.no_curriculum, curriculum_type=args.curr_type,
                                  code_code_pairs_path=args.code_code_pairs_path, valid_steps=args.valid_steps)
    metrics_path = os.path.join(args.exp_name, "train_metrics.json")
    print(f"saving metrics to {metrics_path}")
    with open(metrics_path, "w") as f:
        json.dump(metrics, f)
# def train_classifier(args):
#     import os
#     print("\x1b[33;1mtraining relevance classifier\x1b[0m")
#     print("initializing model and tokenizer ..")
#     tok_path = models.get_tok_path("codebert")
#     print("creating model object")
#     rel_classifier = CodeBERTRelevanceClassifier(tok_path=tok_path)
#     print("commencing training")
#     metrics = rel_classifier.fit(train_path=args.train_path, 
#                                  device_id=args.device_id,
#                                  val_path=args.val_path, 
#                                  exp_name=args.exp_name,
#                                  epochs=5)
#     metrics_path = os.path.join(args.exp_name, "train_metrics.json")
#     print(f"saving metrics to {metrics_path}")
#     with open(metrics_path, "w") as f:
#         json.dump(metrics, f)
# def train_regressor(args):
#     import os
#     print("\x1b[33;1mtraining relevance regressor\x1b[0m")
#     print("initializing model and tokenizer ..")
#     tok_path = models.get_tok_path("codebert")
#     print("creating model object")
#     rel_regressor = CodeBERTRelevanceRegressor(tok_path=tok_path)
#     print("commencing training")
#     metrics = rel_regressor.fit(train_path=args.train_path, 
#                                 device_id=args.device_id,
#                                 val_path=args.val_path, 
#                                 exp_name=args.exp_name,
#                                 epochs=5)
#     metrics_path = os.path.join(args.exp_name, "train_metrics.json")
#     print(f"saving metrics to {metrics_path}")
#     with open(metrics_path, "w") as f:
#         json.dump(metrics, f)
# def test_regressor(args):
#     print("\x1b[33;1mtesting relevance regressor\x1b[0m")
#     print("initializing model and tokenizer ..")
#     tok_path = models.get_tok_path("codebert")
#     print("creating model object")
    
#     predict_path = args.predict_path
#     stem, ext = os.path.splitext(predict_path)
#     save_path = f"{stem}_rel_scores{ext}"
#     if os.path.exists(save_path):
#         print(f"""file \x1b[34;1m'{save_path}'\x1b[0m already exists and won't be overwritten. 
#               Please delete the file if you are sure you don't need it to proceed""")
#         return
#     rel_regressor = CodeBERTRelevanceRegressor(tok_path=tok_path)
#     with open(predict_path) as f:
#         triples = json.load(f)
#     q_and_c = []
#     for item in triples:
#         a = item["a"]
#         n = item["n"]
#         #  only calculate relevance scores for missing/invalid values (-1)
#         if item["r_an"] == -1:
#             q_and_c.append((a,n))
#     q_and_c = q_and_c[:100]
#     rel_scores = rel_regressor.predict(q_and_c=q_and_c,
#                                        device_id=args.device_id,
#                                        exp_name=args.exp_name,
#                                        batch_size=32)
#     i = 0
#     print("len(rel_scores)=", len(rel_scores))
#     print(rel_scores)
#     for item in triples[:100]:
#         a = item["a"]
#         n = item["n"]
#         #  only calculate relevance scores for missing/invalid values (-1)
#         if item["r_an"] == -1:
#             item["r_an"] = rel_scores[i]
#             i += 1
#     print(f"saving relevance predictions to {save_path}")
#     with open(save_path, "w") as f:
#         json.dump(triples, f, indent=4)
def test_retreival(args):
    print("initializing model and tokenizer ..")
    tok_path = os.path.join(os.path.expanduser("~"), "codebert-base-tok")
    device = args.device_id if torch.cuda.is_available() else "cpu"
    
    ckpt_path = os.path.join(args.exp_name, "model.pt")
    print(f"loading checkpoint (state dict) from {ckpt_path}")
    try: state_dict = torch.load(ckpt_path, map_location="cpu")
    except Exception as e: 
        state_dict = None
        print("Couldn't load state dict because:")
        print(e)
    
    print("creating model object")
    triplet_net = CodeBERTripletNet(tok_path=tok_path, **vars(args))
    if state_dict: 
        print(f"\x1b[32;1mloading state dict from {ckpt_path}\x1b[0m")
        triplet_net.load_state_dict(state_dict)
    print(f"loading candidates from {args.candidates_path}")
    code_and_annotations = json.load(open(args.candidates_path))
    
    for setting in ["code", "annot", "code+annot"]:
        if setting == "code":
            candidates = code_and_annotations["snippets"]
        elif setting == "annot":
            candidates = code_and_annotations["annotations"]
        else: # use both code and annotations.
            code_candidates = code_and_annotations["snippets"]
            annot_candidates = code_and_annotations["annotations"]
            candidates = code_candidates

        print(f"loading queries from {args.queries_path}")
        queries_and_cand_labels = json.load(open(args.queries_path))
        queries = [i["query"] for i in queries_and_cand_labels]
        labels = [i["docs"] for i in queries_and_cand_labels]
        # dist_func = "l2_dist"
        for dist_func in ["l2_dist", "inner_prod"]:
            metrics_path = os.path.join(args.exp_name, f"test_metrics_{dist_func}_{setting}.json")
            # if dist_func in ["l2_dist", "inner_prod"]:
            print(f"encoding {len(queries)} queries:")
            query_mat = triplet_net.encode_emb(queries, mode="text", 
                                               batch_size=args.batch_size,
                                               use_tqdm=True, device_id=device)
            query_mat = torch.stack(query_mat)

            print(f"encoding {len(candidates)} candidates:")
            if setting == "code":
                cand_mat = triplet_net.encode_emb(candidates, mode="code", 
                                                  batch_size=args.batch_size,
                                                  use_tqdm=True, device_id=device)
                cand_mat = torch.stack(cand_mat)
            elif setting == "annot":
                cand_mat = triplet_net.encode_emb(candidates, mode="text", 
                                                  batch_size=args.batch_size,
                                                  use_tqdm=True, device_id=device)
                cand_mat = torch.stack(cand_mat)
            else:
                cand_mat_code = triplet_net.encode_emb(code_candidates, mode="code", 
                                                       batch_size=args.batch_size,
                                                       use_tqdm=True, device_id=device)
                cand_mat_annot = triplet_net.encode_emb(annot_candidates, mode="text",
                                                        batch_size=args.batch_size,
                                                        use_tqdm=True, device_id=device)
                cand_mat_code = torch.stack(cand_mat_code)
                cand_mat_annot = torch.stack(cand_mat_annot)
                    # cand_mat = (cand_mat_code + cand_mat_annot)/2
            # print(query_mat.shape, cand_mat.shape)
            if dist_func == "inner_prod": 
                if setting == "code+annot":
                    scores_code = query_mat @ cand_mat_code.T
                    scores_annot = query_mat @ cand_mat_annot.T
                    scores = scores_code + scores_annot
                else:
                    scores = query_mat @ cand_mat.T
                # print(scores.shape)
            elif dist_func == "l2_dist": 
                if setting == "code+annot":
                    scores_code = torch.cdist(query_mat, cand_mat_code, p=2)
                    scores_annot = torch.cdist(query_mat, cand_mat_annot, p=2)
                    scores = scores_code + scores_annot
                else:
                    scores = torch.cdist(query_mat, cand_mat, p=2)
            # elif mode == "joint_cls": scores = triplet_net.joint_classify(queries, candidates)
            doc_ranks = scores.argsort(axis=1)
            if dist_func == "inner_prod":
                doc_ranks = doc_ranks.flip(dims=[1])
            label_ranks = []
            avg_rank = 0
            avg_best_rank = 0 
            N = 0
            M = 0

            lrap_GT = np.zeros(
                (
                    len(queries), 
                    len(candidates)
                )
            )
            recall_at_ = []
            for i in range(1,10+1):
                recall_at_.append(
                    recall_at_k(
                        labels, 
                        doc_ranks.tolist(), 
                        k=5*i
                    )
                )
            for i in range(len(labels)):
                for j in labels[i]:
                    lrap_GT[i][j] = 1

            for i, rank_list in enumerate(doc_ranks):
                rank_list = rank_list.tolist()
                # if dist_func == "inner_prod": rank_list = rank_list.tolist()[::-1]
                # elif dist_func == "l2_dist": rank_list = rank_list.tolist()
                instance_label_ranks = []
                ranks = []
                for cand_rank in labels[i]:
                    # print(rank_list, cand_rank)
                    rank = rank_list.index(cand_rank)
                    avg_rank += rank
                    ranks.append(rank)
                    N += 1
                    instance_label_ranks.append(rank)
                M += 1
                avg_best_rank += min(ranks)
                label_ranks.append(instance_label_ranks)
            metrics = {
                "avg_candidate_rank": avg_rank/N,
                "avg_best_candidate_rank": avg_best_rank/M,
                "recall": {
                    f"@{5*i}": recall_at_[i-1] for i in range(1,10+1) 
                },
            }
            print("avg canditate rank:", avg_rank/N)
            print("avg best candidate rank:", avg_best_rank/M)
            for i in range(1,10+1):
                print(f"recall@{5*i} = {recall_at_[i-1]}")
            if dist_func == "inner_prod":
                # -scores for distance based scores, no - for innert product based scores.
                mrr = MRR(lrap_GT, scores.cpu().numpy())
                ndcg = NDCG(lrap_GT, scores.cpu().numpy())
            elif dist_func == "l2_dist":
                # -scores for distance based scores, no - for innert product based scores.
                mrr = MRR(lrap_GT, -scores.cpu().numpy())
                ndcg = NDCG(lrap_GT, -scores.cpu().numpy())
                
            metrics["mrr"] = mrr
            metrics["ndcg"] = ndcg
            print("NDCG:", ndcg)
            print("MRR (LRAP):", mrr)
            if not os.path.exists(args.exp_name):
                print("missing experiment folder: assuming zero-shot setting")
                metrics_path = os.path.join(
                    "CodeBERT_zero_shot", 
                    f"test_metrics_{dist_func}_{setting}.json"
                )
                os.makedirs("CodeBERT_zero_shot", exist_ok=True)
            with open(metrics_path, "w") as f:
                json.dump(metrics, f)

                
if __name__ == "__main__":
    args = get_args()
    # if args.train_cls: # do relevance classification.
    #     train_classifier(args)
    # if args.test_cls: # test relevance classification.
    #     test_classifier(args)
    # if args.train_rel: # do regression.
    #     train_regressor(args)
    # if args.test_rel: # test regression,
    #     test_regressor(args)
    if args.train: main(args) # finetune.
    if args.test: test_retreival(args) # setting in ['code', 'annot', 'code+annot']
    if args.test_ood: 
        print("creating model object")
        # instantiate model class.
        tok_path = get_tok_path("codebert")
        triplet_net = CodeBERTripletNet(tok_path=tok_path, **vars(args))
        test_ood_performance(
            triplet_net, model_name="codebert", args=args,
            query_paths=["query_and_candidates.json", "external_knowledge/queries.json", 
                         "data/queries_webquery.json", "data/queries_codesearchnet.json"],
            cand_paths=["candidate_snippets.json", "external_knowledge/candidates.json",
                        "data/candidates_webquery.json", "data/candidates_codesearchnet.json"], 
        )