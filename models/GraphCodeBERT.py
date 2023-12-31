#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Atharva Naik - finetuning and model code.
# Soumitra Das - changes to Dataset classes for GraphCodeBERT
import os
import json
import time
import torch
import random
import argparse
import numpy as np
import torch.nn as nn
from tqdm import tqdm
from torch.optim import AdamW
from typing import Union, List
from tree_sitter import Language, Parser
from sklearn.metrics import ndcg_score as NDCG
from torch.utils.data import Dataset, DataLoader
from transformers import RobertaModel, RobertaTokenizer
from datautils import ValRetDataset, CodeRetrieverDataset
from models.metrics import recall_at_k, TripletAccuracy, RuleWiseAccuracy
from sklearn.metrics import label_ranking_average_precision_score as MRR
from datautils.parser import DFG_python
from datautils.parser import (remove_comments_and_docstrings,
                              tree_to_token_index,
                              index_to_code_token,
                              tree_to_variable_index)
from models import test_ood_performance, get_tok_path, dynamic_negative_sampling, fit_disco
from models.losses import scl_loss, TripletMarginWithDistanceLoss, cos_dist, cos_cdist, cos_csim
# seed
random.seed(0)
np.random.seed(0)
torch.manual_seed(0)
# global variables. TODO: add to argparse.
VALID_STEPS = 501
SHUFFLE_BATCH_DEBUG_SETTING = False
print(f"\x1b[31;1mUSING BATCH SHUFFLE = {SHUFFLE_BATCH_DEBUG_SETTING}\x1b[0m")
# get arguments
def get_args():
    parser = argparse.ArgumentParser("""script to train (using triplet margin loss), evaluate and predict with 
                                     the GraphCodeBERT in Late Fusion configuration for Neural Code Search.""")    
    parser.add_argument("-en", "--exp_name", type=str, default="triplet_CodeBERT_rel_thresh", help="experiment name (will be used as folder name)")
    parser.add_argument("-c", "--candidates_path", type=str, default="candidate_snippets.json", help="path to candidates (to test retrieval)")
    parser.add_argument("-q", "--queries_path", type=str, default="query_and_candidates.json", help="path to queries (to test retrieval)")
    parser.add_argument("-tp", "--train_path", type=str, default="triples/triples_train_fixed.json", help="path to training triplet data")
    parser.add_argument("-vp", "--val_path", type=str, default="triples/triples_test_fixed.json", help="path to validation triplet data")
    parser.add_argument("-d", "--device_id", type=str, default="cpu", help="device string (GPU) for doing training/testing")
    parser.add_argument("-lr", "--lr", type=float, default=1e-5, help="learning rate for training (defaults to 1e-5)")
    parser.add_argument("-pe", "--predict", action="store_true", help="flag to do prediction/testing")
    parser.add_argument("-t", "--train", action="store_true", help="flag to do training")
    parser.add_argument("-bs", "--batch_size", type=int, default=32, help="batch size")
    parser.add_argument("-e", "--epochs", type=int, default=5, help="no. of epochs")
    parser.add_argument("-too", "--test_ood", action="store_true", help="flat to do ood testing")
    parser.add_argument("-crb", "--code_retriever_baseline", action="store_true", help="use CodeRetriever objective")
    parser.add_argument("-crt", "--code_retriever_triplets", action="store_true", help="use CodeRetriever bimodal objective with random triplets")
    parser.add_argument("-dns", "--dynamic_negative_sampling", action="store_true", 
                        help="do dynamic negative sampling at batch level")
    parser.add_argument("-sip", "--sim_intents_path", type=str, default=None, 
                        help="path to dictionary containing similar intents corresponding to a given intent")
    parser.add_argument("-pcp", "--perturbed_codes_path", type=str, default=None, 
                        help="path to dictionary containing AST perturbed codes corresponding to a given code")
    parser.add_argument("-ccpp", "--code_code_pairs_path", type=str, default=None, 
                        help="path to code-code pairs for CodeRetriever's unimodal objective")
    parser.add_argument("-w", "--warmup_steps", type=int, default=3000, help="no. of warmup steps (soft negatives only during warmup)")
    parser.add_argument("-p", "--p", type=int, default=2, help="the p used in mastering rate")
    parser.add_argument("-nc", "--no_curriculum", action="store_true", help="turn of curriclum (only hard negatives)")
    parser.add_argument("-rc", "--rand_curriculum", action="store_true", help="random curriculum: equal probability of hard and soft negatives")
    parser.add_argument("-beta", "--beta", type=float, default=0.01, help="the beta used in the von-Mises fisher sampling")
    parser.add_argument("-ast", "--use_AST", action="store_true", help="use AST perturbed negative samples")
    parser.add_argument("-idns", "--intent_level_dynamic_sampling", action="store_true", 
                        help="dynamic sampling based on similar intents")
    parser.add_argument("-uce", "--use_cross_entropy", action="store_true", help="use cross entropy loss instead of triplet margin loss")
    parser.add_argument("-disco", "--disco_baseline", action="store_true", help="use DISCO training procedure")
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
    parser.add_argument("-discr", "--use_disco_rules", action='store_true',
                        help="use the rules outlined in/inspired by the DISCO paper (9)")
    parser.add_argument("-ccl", "--use_ccl", action="store_true", help="use code contrastive loss for hard negatives")
    parser.add_argument("-csim", "--use_csim", action="store_true", help="cosine similarity instead of euclidean distance")
    args = parser.parse_args()
    if args.use_cross_entropy and args.curr_type not in ["soft", "hard"]:
        args.curr_type = "hard"
    if args.use_ccl: args.curr_type = "hard"
    assert not(args.use_ccl and args.use_cross_entropy), "conflicting objectives selected: CCL and CE CL"
    assert not(args.use_ccl and args.code_retriever_baseline), "conflicting objectives selected: CCL and CodeRetriever"
    if args.code_retriever_baseline: # only use soft negative for CodeRetriever
        args.curr_type = "soft"

    return args
    
# wrapper model to make GraphCodeBERT work.
class GraphCodeBERTWrapperModel(nn.Module):   
    def __init__(self, encoder):
        super(GraphCodeBERTWrapperModel, self).__init__()
        self.encoder = encoder
        
    def forward(self, code_inputs=None, attn_mask=None, position_idx=None, nl_inputs=None): 
        if code_inputs is not None:
            # uses position_idx.
            nodes_mask=position_idx.eq(0)
            token_mask=position_idx.ge(2)        
            inputs_embeddings=self.encoder.embeddings.word_embeddings(code_inputs)
            nodes_to_token_mask=nodes_mask[:,:,None]&token_mask[:,None,:]&attn_mask
            nodes_to_token_mask=nodes_to_token_mask/(nodes_to_token_mask.sum(-1)+1e-10)[:,:,None]
            avg_embeddings=torch.einsum("abc,acd->abd",nodes_to_token_mask,inputs_embeddings)
            inputs_embeddings=inputs_embeddings*(~nodes_mask)[:,:,None]+avg_embeddings*nodes_mask[:,:,None]    
            return self.encoder(inputs_embeds=inputs_embeddings, attention_mask=attn_mask, position_ids=position_idx)[1]
        else: return self.encoder(nl_inputs, attention_mask=nl_inputs.ne(1))[1]

# code dataset.
class CodeDataset(Dataset):
    def __init__(self, code_snippets: str,  args: dict, tokenizer: Union[str, None, RobertaTokenizer]=None):
        super(CodeDataset, self).__init__()
        self.data = code_snippets
        self.args = args
        LANGUAGE = Language('datautils/parser/py_parser.so', 'python')
        PARSER =  Parser()
        PARSER.set_language(LANGUAGE)
        self.parser = [PARSER, DFG_python]
        if isinstance(tokenizer, RobertaTokenizer): self.tokenizer = tokenizer
        elif isinstance(tokenizer, str):
            self.tokenizer = RobertaTokenizer.from_pretrained(tokenizer)
        else: self.tokenizer = tokenizer
    
    def __len__(self):
        return len(self.data)
    
    def proc_code(self, code: str):
        # try:
        try: code = remove_comments_and_docstrings(code, 'python')
        except: pass
        # except:
        #    print(f"error in removing comments and docstrings: {code}")
        # print(type(code))
        tree = self.parser[0].parse(bytes(code,'utf8'))    
        root_node = tree.root_node  
        tokens_index=tree_to_token_index(root_node)     
        code=code.split('\n')
        code_tokens=[index_to_code_token(x,code) for x in tokens_index]  
        index_to_code={}
        for idx,(index,code) in enumerate(zip(tokens_index,code_tokens)):
            index_to_code[index]=(idx,code)  
        try: DFG,_=self.parser[1](root_node,index_to_code,{}) 
        except Exception as e: print("Ln 246:", e); DFG=[]
        # except Exception as e:
        #     print("Parsing error:", e)
        #     DFG=[]
        DFG=sorted(DFG,key=lambda x:x[1])
        indexs=set()
        for d in DFG:
            if len(d[-1])!=0: indexs.add(d[1])
            for x in d[-1]: indexs.add(x)
        new_DFG=[]
        for d in DFG:
            if d[1] in indexs: new_DFG.append(d)
        dfg=new_DFG 
        
        return code_tokens, dfg
    
    def __getitem__(self, item: int):
        tokenizer = self.tokenizer
        args = self.args
        code = self.data[item]
        code_tokens, dfg=self.proc_code(code)
        code_tokens=[tokenizer.tokenize('@ '+x)[1:] if idx!=0 else tokenizer.tokenize(x) for idx,x in enumerate(code_tokens)]
        ori2cur_pos={}
        ori2cur_pos[-1]=(0,0)
        for i in range(len(code_tokens)):
            ori2cur_pos[i]=(ori2cur_pos[i-1][1],ori2cur_pos[i-1][1]+len(code_tokens[i]))    
        code_tokens=[y for x in code_tokens for y in x]  
        #truncating
        code_tokens=code_tokens[:args["code_length"]+args["data_flow_length"]-2-min(len(dfg),args["data_flow_length"])]
        code_tokens =[tokenizer.cls_token]+code_tokens+[tokenizer.sep_token]
        code_ids =  tokenizer.convert_tokens_to_ids(code_tokens)
        position_idx = [i+tokenizer.pad_token_id + 1 for i in range(len(code_tokens))]
        dfg=dfg[:args["code_length"]+args["data_flow_length"]
                -len(code_tokens)]
        code_tokens+=[x[0] for x in dfg]
        position_idx+=[0 for x in dfg]
        code_ids+=[tokenizer.unk_token_id for x in dfg]
        padding_length=args["code_length"]+args["data_flow_length"]-len(code_ids)
        position_idx+=[tokenizer.pad_token_id]*padding_length
        code_ids+=[tokenizer.pad_token_id]*padding_length    
        #reindex
        reverse_index={}
        for idx,x in enumerate(dfg):
            reverse_index[x[1]]=idx
        for idx,x in enumerate(dfg):
            dfg[idx]=x[:-1]+([reverse_index[i] for i in x[-1] if i in reverse_index],)    
        dfg_to_dfg=[x[-1] for x in dfg]
        dfg_to_code=[ori2cur_pos[x[1]] for x in dfg]
        length=len([tokenizer.cls_token])
        dfg_to_code=[(x[0]+length,x[1]+length) for x in dfg_to_code] 

        #calculate graph-guided masked function
        attn_mask=np.zeros((self.args["code_length"]+self.args["data_flow_length"],
                            self.args["code_length"]+self.args["data_flow_length"]),dtype=bool)
        #calculate begin index of node and max length of input
        node_index=sum([i>1 for i in position_idx])
        max_length=sum([i!=1 for i in position_idx])
        #sequence can attend to sequence
        attn_mask[:node_index,:node_index]=True
        #special tokens attend to all tokens
        for idx,i in enumerate(code_ids):
            if i in [0,2]:
                attn_mask[idx,:max_length]=True
        #nodes attend to code tokens that are identified from
        for idx,(a,b) in enumerate(dfg_to_code):
            if a<node_index and b<node_index:
                attn_mask[idx+node_index,a:b]=True
                attn_mask[a:b,idx+node_index]=True
        #nodes attend to adjacent nodes 
        for idx,nodes in enumerate(dfg_to_dfg):
            for a in nodes:
                if a+node_index<len(position_idx):
                    attn_mask[idx+node_index,a+node_index]=True  
                    
        return (torch.tensor(code_ids),
                torch.tensor(attn_mask),
                torch.tensor(position_idx))    
    
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
            return [text["input_ids"][0]]
        else:
            return [text]
        
        
class TextCodePairDataset(Dataset):
    def __init__(self, texts: str, codes: str, args: dict, tokenizer: Union[str, None, RobertaTokenizer]=None):
        super(TextCodePairDataset, self).__init__()
        self.data = [(text, code) for text, code in zip(texts, codes)]
        self.args = args
        LANGUAGE = Language('datautils/parser/py_parser.so', 'python')
        PARSER =  Parser()
        PARSER.set_language(LANGUAGE)
        self.parser = [PARSER, DFG_python]
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
        # print(type(code))
        tree = self.parser[0].parse(bytes(code,'utf8'))    
        root_node = tree.root_node  
        tokens_index=tree_to_token_index(root_node)     
        code=code.split('\n')
        code_tokens=[index_to_code_token(x,code) for x in tokens_index]  
        index_to_code={}
        for idx,(index,code) in enumerate(zip(tokens_index,code_tokens)):
            index_to_code[index]=(idx,code)  
        try:
            DFG,_=self.parser[1](root_node,index_to_code,{}) 
        except Exception as e:
            print("Ln 246:", e)
            DFG=[]
        DFG=sorted(DFG,key=lambda x:x[1])
        indexs=set()
        for d in DFG:
            if len(d[-1])!=0:
                indexs.add(d[1])
            for x in d[-1]:
                indexs.add(x)
        new_DFG=[]
        for d in DFG:
            if d[1] in indexs:
                new_DFG.append(d)
        dfg=new_DFG 
        return code_tokens,dfg
    
    def proc_text(self, text: str):
        text = " ".join(text.split("\n"))
        text = " ".join(text.split()).strip()
        return text
    
    def __getitem__(self, item: int):
        tokenizer = self.tokenizer
        args = self.args
        text = self.data[item][0]
        code = self.data[item][1]

        code_tokens,dfg=self.proc_code(code)
        code_tokens=[tokenizer.tokenize('@ '+x)[1:] if idx!=0 else tokenizer.tokenize(x) for idx,x in enumerate(code_tokens)]
        ori2cur_pos={}
        ori2cur_pos[-1]=(0,0)
        for i in range(len(code_tokens)):
            ori2cur_pos[i]=(ori2cur_pos[i-1][1],ori2cur_pos[i-1][1]+len(code_tokens[i]))    
        code_tokens=[y for x in code_tokens for y in x]  
        #truncating
        code_tokens=code_tokens[:args["code_length"]+args["data_flow_length"]-2-min(len(dfg),args["data_flow_length"])]
        code_tokens =[tokenizer.cls_token]+code_tokens+[tokenizer.sep_token]
        code_ids =  tokenizer.convert_tokens_to_ids(code_tokens)
        position_idx = [i+tokenizer.pad_token_id + 1 for i in range(len(code_tokens))]
        dfg=dfg[:args["code_length"]+args["data_flow_length"]
                -len(code_tokens)]
        code_tokens+=[x[0] for x in dfg]
        position_idx+=[0 for x in dfg]
        code_ids+=[tokenizer.unk_token_id for x in dfg]
        padding_length=args["code_length"]+args["data_flow_length"]-len(code_ids)
        position_idx+=[tokenizer.pad_token_id]*padding_length
        code_ids+=[tokenizer.pad_token_id]*padding_length    
        #reindex
        reverse_index={}
        for idx,x in enumerate(dfg):
            reverse_index[x[1]]=idx
        for idx,x in enumerate(dfg):
            dfg[idx]=x[:-1]+([reverse_index[i] for i in x[-1] if i in reverse_index],)    
        dfg_to_dfg=[x[-1] for x in dfg]
        dfg_to_code=[ori2cur_pos[x[1]] for x in dfg]
        length=len([tokenizer.cls_token])
        dfg_to_code=[(x[0]+length,x[1]+length) for x in dfg_to_code]  
        #nl
        nl=self.proc_text(text)
        nl_tokens=tokenizer.tokenize(nl)[:args["nl_length"]-2]
        nl_tokens =[tokenizer.cls_token]+nl_tokens+[tokenizer.sep_token]
        nl_ids =  tokenizer.convert_tokens_to_ids(nl_tokens)
        padding_length = args["nl_length"] - len(nl_ids)
        nl_ids+=[tokenizer.pad_token_id]*padding_length

        #calculate graph-guided masked function
        attn_mask=np.zeros((self.args["code_length"]+self.args["data_flow_length"],
                            self.args["code_length"]+self.args["data_flow_length"]),dtype=bool)
        #calculate begin index of node and max length of input
        node_index=sum([i>1 for i in position_idx])
        max_length=sum([i!=1 for i in position_idx])
        #sequence can attend to sequence
        attn_mask[:node_index,:node_index]=True
        #special tokens attend to all tokens
        for idx,i in enumerate(code_ids):
            if i in [0,2]:
                attn_mask[idx,:max_length]=True
        #nodes attend to code tokens that are identified from
        for idx,(a,b) in enumerate(dfg_to_code):
            if a<node_index and b<node_index:
                attn_mask[idx+node_index,a:b]=True
                attn_mask[a:b,idx+node_index]=True
        #nodes attend to adjacent nodes 
        for idx,nodes in enumerate(dfg_to_dfg):
            for a in nodes:
                if a+node_index<len(position_idx):
                    attn_mask[idx+node_index,a+node_index]=True 

        return (torch.tensor(code_ids),
                torch.tensor(attn_mask),
                torch.tensor(position_idx),
                torch.tensor(nl_ids))
        
        
class TriplesDataset(Dataset):
    def __init__(self, path: str, args: dict, 
                 tokenizer: Union[str, None, RobertaTokenizer]=None):
        super(TriplesDataset, self).__init__()
        self.data = json.load(open(path))
        self.args = args
        LANGUAGE = Language('datautils/parser/py_parser.so', 'python')
        PARSER =  Parser()
        PARSER.set_language(LANGUAGE)
        self.parser = [PARSER, DFG_python]
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
        tree = self.parser[0].parse(bytes(code, 'utf8'))    
        root_node = tree.root_node  
        tokens_index=tree_to_token_index(root_node)     
        code=code.split('\n')
        code_tokens=[index_to_code_token(x,code) for x in tokens_index]  
        index_to_code={}
        for idx,(index,code) in enumerate(zip(tokens_index,code_tokens)):
            index_to_code[index]=(idx,code)  
        try:
            DFG,_ = self.parser[1](root_node,index_to_code,{}) 
        except Exception as e:
            print("Ln 380:", e)
            DFG=[]
        DFG=sorted(DFG,key=lambda x:x[1])
        indexs=set()
        for d in DFG:
            if len(d[-1])!=0:
                indexs.add(d[1])
            for x in d[-1]:
                indexs.add(x)
        new_DFG=[]
        for d in DFG:
            if d[1] in indexs:
                new_DFG.append(d)
        dfg=new_DFG 
        
        return code_tokens, dfg
        
    def __getitem__(self, item: int):
        tokenizer = self.tokenizer
        args = self.args
        text = self.data[item][0]
        pos = self.data[item][1]
        neg = self.data[item][2]
        # nl
        nl=self.proc_text(text)
        nl_tokens=tokenizer.tokenize(nl)[:args["nl_length"]-2]
        nl_tokens =[tokenizer.cls_token]+nl_tokens+[tokenizer.sep_token]
        nl_ids =  tokenizer.convert_tokens_to_ids(nl_tokens)
        padding_length = args["nl_length"] - len(nl_ids)
        nl_ids+=[tokenizer.pad_token_id]*padding_length 
        # pos
        code_tokens,dfg=self.proc_code(pos)
        code_tokens=[tokenizer.tokenize('@ '+x)[1:] if idx!=0 else tokenizer.tokenize(x) for idx,x in enumerate(code_tokens)]
        ori2cur_pos={}
        ori2cur_pos[-1]=(0,0)
        for i in range(len(code_tokens)):
            ori2cur_pos[i]=(ori2cur_pos[i-1][1],ori2cur_pos[i-1][1]+len(code_tokens[i]))    
        code_tokens=[y for x in code_tokens for y in x]  
        # truncating
        code_tokens=code_tokens[:args["code_length"]+args["data_flow_length"]-2-min(len(dfg),args["data_flow_length"])]
        code_tokens =[tokenizer.cls_token]+code_tokens+[tokenizer.sep_token]
        pos_code_ids =  tokenizer.convert_tokens_to_ids(code_tokens)
        pos_position_idx = [i+tokenizer.pad_token_id + 1 for i in range(len(code_tokens))]
        dfg=dfg[:args["code_length"]+args["data_flow_length"]
                -len(code_tokens)]
        code_tokens+=[x[0] for x in dfg]
        pos_position_idx+=[0 for x in dfg]
        pos_code_ids+=[tokenizer.unk_token_id for x in dfg]
        padding_length=args["code_length"]+args["data_flow_length"]-len(pos_code_ids)
        pos_position_idx+=[tokenizer.pad_token_id]*padding_length
        pos_code_ids+=[tokenizer.pad_token_id]*padding_length    
        # reindex
        reverse_index={}
        for idx,x in enumerate(dfg):
            reverse_index[x[1]]=idx
        for idx,x in enumerate(dfg):
            dfg[idx]=x[:-1]+([reverse_index[i] for i in x[-1] if i in reverse_index],)    
        dfg_to_dfg=[x[-1] for x in dfg]
        dfg_to_code=[ori2cur_pos[x[1]] for x in dfg]
        length=len([tokenizer.cls_token])
        dfg_to_code=[(x[0]+length,x[1]+length) for x in dfg_to_code] 

        # calculate graph-guided masked function
        pos_attn_mask=np.zeros((self.args["code_length"]+self.args["data_flow_length"],
                            self.args["code_length"]+self.args["data_flow_length"]),dtype=bool)
        # calculate begin index of node and max length of input
        node_index=sum([i>1 for i in pos_position_idx])
        max_length=sum([i!=1 for i in pos_position_idx])
        # sequence can attend to sequence
        pos_attn_mask[:node_index,:node_index]=True
        # special tokens attend to all tokens
        for idx,i in enumerate(pos_code_ids):
            if i in [0,2]:
                pos_attn_mask[idx,:max_length]=True
        # nodes attend to code tokens that are identified from
        for idx,(a,b) in enumerate(dfg_to_code):
            if a<node_index and b<node_index:
                pos_attn_mask[idx+node_index,a:b]=True
                pos_attn_mask[a:b,idx+node_index]=True
        # nodes attend to adjacent nodes 
        for idx,nodes in enumerate(dfg_to_dfg):
            for a in nodes:
                if a+node_index<len(pos_position_idx):
                    pos_attn_mask[idx+node_index,a+node_index]=True

        # neg
        code_tokens,dfg=self.proc_code(neg)
        code_tokens=[tokenizer.tokenize('@ '+x)[1:] if idx!=0 else tokenizer.tokenize(x) for idx,x in enumerate(code_tokens)]
        ori2cur_pos={}
        ori2cur_pos[-1]=(0,0)
        for i in range(len(code_tokens)):
            ori2cur_pos[i]=(ori2cur_pos[i-1][1],ori2cur_pos[i-1][1]+len(code_tokens[i]))    
        code_tokens=[y for x in code_tokens for y in x]  
        # truncating
        code_tokens=code_tokens[:args["code_length"]+args["data_flow_length"]-2-min(len(dfg),args["data_flow_length"])]
        code_tokens =[tokenizer.cls_token]+code_tokens+[tokenizer.sep_token]
        neg_code_ids =  tokenizer.convert_tokens_to_ids(code_tokens)
        neg_position_idx = [i+tokenizer.pad_token_id + 1 for i in range(len(code_tokens))]
        dfg=dfg[:args["code_length"]+args["data_flow_length"]
                -len(code_tokens)]
        code_tokens+=[x[0] for x in dfg]
        neg_position_idx+=[0 for x in dfg]
        neg_code_ids+=[tokenizer.unk_token_id for x in dfg]
        padding_length=args["code_length"]+args["data_flow_length"]-len(neg_code_ids)
        neg_position_idx+=[tokenizer.pad_token_id]*padding_length
        neg_code_ids+=[tokenizer.pad_token_id]*padding_length    
        # reindex
        reverse_index={}
        for idx,x in enumerate(dfg):
            reverse_index[x[1]]=idx
        for idx,x in enumerate(dfg):
            dfg[idx]=x[:-1]+([reverse_index[i] for i in x[-1] if i in reverse_index],)    
        dfg_to_dfg=[x[-1] for x in dfg]
        dfg_to_code=[ori2cur_pos[x[1]] for x in dfg]
        length=len([tokenizer.cls_token])
        dfg_to_code=[(x[0]+length,x[1]+length) for x in dfg_to_code] 

        # calculate graph-guided masked function
        neg_attn_mask=np.zeros((self.args["code_length"]+self.args["data_flow_length"],
                            self.args["code_length"]+self.args["data_flow_length"]),dtype=bool)
        # calculate begin index of node and max length of input
        node_index=sum([i>1 for i in neg_position_idx])
        max_length=sum([i!=1 for i in neg_position_idx])
        # sequence can attend to sequence
        neg_attn_mask[:node_index,:node_index]=True
        # special tokens attend to all tokens
        for idx,i in enumerate(neg_code_ids):
            if i in [0,2]:
                neg_attn_mask[idx,:max_length]=True
        # nodes attend to code tokens that are identified from
        for idx,(a,b) in enumerate(dfg_to_code):
            if a<node_index and b<node_index:
                neg_attn_mask[idx+node_index,a:b]=True
                neg_attn_mask[a:b,idx+node_index]=True
        # nodes attend to adjacent nodes 
        for idx,nodes in enumerate(dfg_to_dfg):
            for a in nodes:
                if a+node_index<len(neg_position_idx):
                    neg_attn_mask[idx+node_index,a+node_index]=True

        return (
                torch.tensor(pos_code_ids),
                torch.tensor(pos_attn_mask),
                torch.tensor(pos_position_idx),
                torch.tensor(neg_code_ids),
                torch.tensor(neg_attn_mask),
                torch.tensor(neg_position_idx),
                torch.tensor(nl_ids)
               )

    
class GraphCodeBERTripletNet(nn.Module):
    """ Class to 
    1) finetune GraphCodeBERT in a late fusion setting using triplet margin loss.
    2) Evaluate metrics on unseen test set.
    3) 
    """
    def __init__(self, model_path: str="microsoft/graphcodebert-base", 
                 tok_path: str="microsoft/graphcodebert-base", **args):
        super(GraphCodeBERTripletNet, self).__init__()
        self.config = {}
        self.config["model_path"] = model_path
        self.config["tok_path"] = tok_path
        
        print(f"loading pretrained GraphCodeBERT embedding model from {model_path}")
        start = time.time()
        self.embed_model = GraphCodeBERTWrapperModel(
            RobertaModel.from_pretrained(model_path)
        )
        print(f"loaded embedding model in {(time.time()-start):.2f}s")
        print(f"loaded tokenizer files from {tok_path}")
        # create tokenizer.
        self.tokenizer = RobertaTokenizer.from_pretrained(tok_path)
        # optimizer and loss.
        adam_eps = 1e-8
        lr = args.get("lr", 1e-5)
        margin = args.get("margin", 1)
        dist_fn_deg = args.get("dist_fn_deg", 2)
        # print optimizer and loss function.
        print(f"optimizer = AdamW(lr={lr}, eps={adam_eps})")
        print(f"loss_fn = TripletMarginLoss(margin={margin}, p={dist_fn_deg})")
        # create optimizer object and loss function.
        self.optimizer = AdamW(
            self.parameters(), 
            eps=adam_eps, lr=lr
        )
        self.loss_fn = nn.TripletMarginLoss(
            p=dist_fn_deg,
            margin=margin, 
            reduction="none",
        )
        print(args)
        # store config info.
        self.ignore_worst_rules = args.get("ignore_worst_rules", False)
        self.ignore_non_disco_rules = args.get("use_disco_rules", False)
        self.code_retriever_baseline = args.get("code_retriever_baseline", False)
        self.use_cross_entropy = args.get("use_cross_entropy", False)
        self.use_ccl = args.get("use_ccl", False)
        self.use_scl = args.get("use_scl", False)
        self.use_csim = args.get("use_csim", False)
        
        self.config["code_retriever_baseline"] = self.code_retriever_baseline
        self.config["use_disco_rules"] = self.ignore_non_disco_rules
        self.config["ignore_worst_rules"] = self.ignore_worst_rules
        self.config["dist_fn_deg"] = dist_fn_deg
        self.config["optimizer"] = f"{self.optimizer}"
        self.config["loss_fn"] = f"{self.loss_fn}"
        self.config["margin"] = margin
        self.config["lr"] = lr
        
        self.dropout1 = nn.Dropout(0.1)
        self.dropout2 = nn.Dropout(0.1)
        self.ce_loss = nn.CrossEntropyLoss()
        
    def forward(self, anchor_title, pos_snippet, neg_snippet):
        anchor_text_emb = self.embed_model(nl_inputs=anchor_title)
        anchor_text_emb = self.embed_model(nl_inputs=anchor_title)
        x = pos_snippet
        pos_code_emb = self.embed_model(code_inputs=x[0], attn_mask=x[1], position_idx=x[2])
        x = neg_snippet
        neg_code_emb = self.embed_model(code_inputs=x[0], attn_mask=x[1], position_idx=x[2])
        
        return anchor_text_emb, pos_code_emb, neg_code_emb
        
    def val(self, valloader: DataLoader, epoch_i: int=0, epochs: int=0, device="cuda:0"):
        self.eval()
        val_acc = TripletAccuracy()
        batch_losses = []
        pbar = tqdm(enumerate(valloader), total=len(valloader), 
                    desc=f"val: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0 acc: 0")
        for step, batch in pbar:
            with torch.no_grad():
                anchor_title = batch[-1].to(device)
                pos_snippet = (batch[0].to(device), batch[1].to(device), batch[2].to(device))
                neg_snippet = (batch[3].to(device), batch[4].to(device), batch[5].to(device))
                anchor_text_emb, pos_code_emb, neg_code_emb = self(anchor_title, pos_snippet, neg_snippet)
                batch_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb)
                val_acc.update(anchor_text_emb, pos_code_emb, neg_code_emb)
                batch_losses.append(batch_loss.item())
                pbar.set_description(f"val: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.3f} loss: {np.mean(batch_losses):.3f} acc: {100*val_acc.get():.2f}")
                # if step == 5: break # DEBUG
        return val_acc.get(), np.mean(batch_losses)
    
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
        if self.use_csim: scores = -cos_csim(query_mat, cand_mat)
        else: scores = torch.cdist(query_mat, cand_mat, p=2)
        doc_ranks = scores.argsort(axis=1)
        recall_at_5 = recall_at_k(labels, doc_ranks.tolist(), k=5)
        
        return recall_at_5
        
    def encode_emb(self, text_or_snippets: List[str], mode: str="text", **args):
        """Note: our late fusion GraphCodeBERT is a universal encoder for text and code, so the same function works for both."""
        batch_size = args.get("batch_size", 32)
        device_id = args.get("device_id", "cuda:0")
        device = torch.device(device_id if torch.cuda.is_available() else "cpu")
        use_tqdm = args.get("use_tqdm", False)
        self.to(device)
        self.eval()
        
        if mode == "text":
            dataset = TextDataset(text_or_snippets, tokenizer=self.tokenizer,
                                  truncation=True, padding="max_length",
                                  max_length=100, add_special_tokens=True,
                                  return_tensors="pt")

        elif mode == "code":
            dataset = CodeDataset(text_or_snippets, 
                                  tokenizer=self.tokenizer,
                                  args={
                                          "nl_length": 100, 
                                          "code_length": 100, 
                                          "data_flow_length": 64
                                       }
                                 )
        else: raise TypeError("Unrecognized encoding mode")
        
        datalloader = DataLoader(dataset, shuffle=False, 
                                 batch_size=batch_size)
        pbar = tqdm(enumerate(datalloader), total=len(datalloader), 
                    desc=f"encoding {mode}", disable=not(use_tqdm))
        all_embeds = []
        for step, batch in pbar:
            with torch.no_grad():
                if mode == "text":
                    nl_inputs = batch[0].to(device)
                    batch_embed = self.embed_model(nl_inputs=nl_inputs)
                elif mode == "code":
                    code_inputs = batch[0].to(device)
                    attn_masks = batch[1].to(device)
                    position_idx = batch[2].to(device)
                    batch_embed = self.embed_model(code_inputs=code_inputs, 
                                                   attn_mask=attn_masks, 
                                                   position_idx=position_idx)
                for embed in batch_embed: 
                    all_embeds.append(embed)
                # if step == 5: break # DEBUG
        # print(type(all_embeds[0]), len(all_embeds))
        # print(len(all_embeds))
        return all_embeds
#     def joint_classify(self, text_snippets: List[str], 
#                        code_snippets: List[str], **args):
#         """The usual joint encoding setup of CodeBERT (similar to NLI)"""
#         batch_size = args.get("batch_size", 48)
#         device_id = args.get("device_id", "cuda:0")
#         device = torch.device(device_id)
#         use_tqdm = args.get("use_tqdm", False)
#         self.to(device)
#         self.eval()
        
#         dataset = TextCodePairDataset(text_snippets, code_snippets, 
#                                       tokenizer=self.tokenizer, truncation=True, 
#                                       padding="max_length", max_length=100, 
#                                       add_special_tokens=True, return_tensors="pt")
#         datalloader = DataLoader(dataset, shuffle=False, 
#                                  batch_size=batch_size)
#         pbar = tqdm(enumerate(datalloader), total=len(datalloader), 
#                     desc=f"enocding {mode}", disable=not(use_tqdm))
#         all_embeds = []
#         for step, batch in pbar:
#             with torch.no_grad():
#                 enc_args = (batch[0].to(device), batch[1].to(device))
#                 batch_embed = self.embed_model(*enc_args).pooler_output
#                 for embed in batch_embed: all_embeds.append(embed)
#                 # if step == 5: break # DEBUG
#         # print(type(all_embeds[0]), len(all_embeds))
#         return all_embeds
    def fit(self, train_path: str, val_path: str, **args):
        use_curriculum = not(args.get("no_curriculum", False))
        if use_curriculum: curriculum_type = "mr"
        rand_curriculum = args.get("rand_curriculum", False)
        if rand_curriculum: curriculum_type = "rand"
        warmup_steps = args.get("warmup_steps", 3000) # NEW
        beta = args.get("beta", 0.01) # NEW
        p = args.get("p") # NEW
        batch_size = args.get("batch_size", 32)
        self.config["batch_size"] = batch_size
        epochs = args.get("epochs", 5)
        self.config["epochs"] = epochs
        device_id = args.get("device_id", "cuda:0")
        self.config["device_id"] = device_id
        device = torch.device(device_id)
        exp_name = args.get("exp_name", "experiment")
        self.config["exp_name"] = exp_name
        os.makedirs(exp_name, exist_ok=True)
        save_path = os.path.join(exp_name, "model.pt")
        self.config["train_path"] = train_path
        self.config["val_path"] = val_path
        
        use_AST = args.get("use_AST", False)
        sim_intents_path = args.get("sim_intents_path")
        code_code_pairs_path = args.get("code_code_pairs_path")
        perturbed_codes_path = args.get("perturbed_codes_path")
        intent_level_dynamic_sampling = args.get("intent_level_dynamic_sampling", False)
        
        self.config["use_ast"] = use_AST
        self.config["sim_intents_path"] = sim_intents_path
        self.config["perturbed_codes_path"] = perturbed_codes_path
        self.config["dynamic_negative_sampling"] = args.get("dynamic_negative_sampling", False)
        self.config["intent_level_dynamic_sampling"] = intent_level_dynamic_sampling

        print(f"model will be saved at {save_path}")
        print(f"moving model to {device}")
        self.embed_model.to(device)
        sim_intents_map = {}
        perturbed_codes = {}
        if intent_level_dynamic_sampling or use_AST:
            from datautils import DynamicTriplesDataset
            if intent_level_dynamic_sampling:
                assert sim_intents_path is not None, "Missing path to dictionary containing similar intents corresponding to an intent"
                sim_intents_map = json.load(open(sim_intents_path))
                perturbed_codes = {}
            if use_AST:
                assert perturbed_codes_path is not None, "Missing path to dictionary containing perturbed codes corresponding to a given code snippet"
                perturbed_codes = json.load(open(perturbed_codes_path))
            trainset = DynamicTriplesDataset(
                train_path, "graphcodebert", device=device_id, beta=beta, p=p, warmup_steps=warmup_steps,
                use_AST=use_AST, model=self, tokenizer=self.tokenizer, sim_intents_map=sim_intents_map, 
                perturbed_codes=perturbed_codes, curriculum_type=curriculum_type,                 
                use_curriculum=use_curriculum, rand_curriculum=rand_curriculum,
                ignore_non_disco_rules=self.ignore_non_disco_rules,
                nl_length=100, code_length=100, data_flow_length=64,
            )
            # valset = ValRetDataset(val_path)
            self.config["trainset.warmup_steps"] = trainset.warmup_steps
            self.config["trainset.epsilon"] = trainset.epsilon
            self.config["trainset.delta"] = trainset.soft_master_rate.delta
            self.config["trainset.beta"] = trainset.beta
            self.config["trainset.p"] = trainset.soft_master_rate.p
        elif self.code_retriever_baseline:    
            trainset = CodeRetrieverDataset(
                train_path, code_code_path=code_code_pairs_path, model_name="graphcodebert", 
                tokenizer=self.tokenizer, nl_length=100, code_length=100, data_flow_length=64,
                # max_length=100, padding="max_length", return_tensors="pt", add_special_tokens=True, truncation=True,
            )
            # valset = ValRetDataset(val_path)
        else:
            trainset = TriplesDataset(train_path, tokenizer=self.tokenizer,
                                      args={
                                              "nl_length": 100, 
                                              "code_length": 100, 
                                              "data_flow_length": 64
                                     })
            # valset = TriplesDataset(val_path, tokenizer=self.tokenizer,
            #                         args={
            #                                "nl_length": 100, 
            #                                "code_length": 100, 
            #                                "data_flow_length": 64
            #                        })
        valset = ValRetDataset(val_path)
        # save config file
        config_path = os.path.join(exp_name, "config.json")
        with open(config_path, "w") as f:
            json.dump(self.config, f)
        print(f"saved config to {config_path}")
        
        if SHUFFLE_BATCH_DEBUG_SETTING and not(self.code_retriever_baseline): #TODO: remove this. Used only for a temporary experiment.
            from datautils import batch_shuffle_collate_fn_graphcodebert
            trainloader = DataLoader(trainset, shuffle=True, batch_size=batch_size,
                                     collate_fn=batch_shuffle_collate_fn_graphcodebert)
            valloader = DataLoader(valset, shuffle=False, batch_size=batch_size,
                                   collate_fn=batch_shuffle_collate_fn_graphcodebert)
        else:
            trainloader = DataLoader(trainset, shuffle=True, 
                                     batch_size=batch_size)
            valloader = DataLoader(valset, shuffle=False,
                                   batch_size=batch_size)
        train_metrics = {
            "log_steps": [],
            "summary": [],
        } 
        rule_wise_acc = RuleWiseAccuracy(margin=1, use_scl=self.use_scl)
        if not(self.use_cross_entropy or self.code_retriever_baseline):
            train_soft_neg_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
            train_hard_neg_acc = TripletAccuracy(margin=1, use_scl=self.use_scl)
        else: 
            train_tot = 0
            train_acc = 0
            train_u_acc = 0
        best_val_acc = 0
        for epoch_i in range(epochs):
            self.train()
            batch_losses = []
            pbar = tqdm(enumerate(trainloader), total=len(trainloader),
                        desc=f"train: epoch: {epoch_i+1}/{epochs} batch_loss: 0 loss: 0 acc: 0")
            rule_wise_acc.reset()
            if not(self.use_cross_entropy or self.code_retriever_baseline):
                train_soft_neg_acc.reset()
                train_hard_neg_acc.reset()
            for step, batch in pbar:
                if args.get("dynamic_negative_sampling", False):
                    batch = dynamic_negative_sampling(
                        self.embed_model, batch, 
                        model_name="graphcodebert", 
                        device=device, k=1
                    )
                self.train()
                anchor_title = batch[6].to(device)
                pos_snippet = (batch[0].to(device), batch[1].to(device), batch[2].to(device))
                neg_snippet = (batch[3].to(device), batch[4].to(device), batch[5].to(device))
                # print(neg_snippet[0].shape, neg_snippet[1].shape, neg_snippet[2].shape)
                anchor_text_emb, pos_code_emb, neg_code_emb = self(anchor_title, pos_snippet, neg_snippet)
                N = len(batch[0])
                if hasattr(trainset, "update") or isinstance(trainset, CodeRetrieverDataset):
                    if not(self.use_cross_entropy or self.code_retriever_baseline):
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
                    if self.use_scl:
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
                    elif self.code_retriever_baseline:
                        if self.use_csim:
                            d_ap = -cos_csim(anchor_text_emb, pos_code_emb)
                            d_pn = -cos_csim(pos_code_emb, neg_code_emb)
                        else:
                            d_ap = torch.cdist(anchor_text_emb, pos_code_emb)
                            d_pn = torch.cdist(pos_code_emb, neg_code_emb)
                        # margin = self.config['margin']*torch.eye(N).to(device)
                        target = torch.as_tensor(range(N)).to(device)
                        unimodal_loss = self.ce_loss(-d_ap, target)
                        bimodal_loss = self.ce_loss(-d_pn, target)
                        # unimodal_loss = self.ce_loss(-(d_ap+margin), target)
                        # bimodal_loss = self.ce_loss(-(d_pn+margin), target)
                        batch_loss = unimodal_loss + bimodal_loss
                        b_preds = (-d_ap).argmax(dim=-1)
                        u_preds = (-d_pn).argmax(dim=-1)
                        train_acc += (b_preds == target).sum().item()
                        train_u_acc += (u_preds == target).sum().item()
                        train_tot += N
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
                    rule_wise_acc.update(anchor_text_emb, pos_code_emb, 
                                         neg_code_emb, batch[-1].cpu().tolist())
                    if (self.use_cross_entropy or self.code_retriever_baseline):
                        pbar.set_description(f"T e:{epoch_i+1}/{epochs} bl:{batch_loss:.3f} l:{np.mean(batch_losses):.3f} {metric_str}")
                    else: 
                        pbar.set_description(
                            f"T e:{epoch_i+1}/{epochs} {MIX_STEP}{batch_loss_str} l:{np.mean(batch_losses):.3f} a:{100*train_soft_neg_acc.get():.2f}{HARD_ACC}"
                        )
                else: 
                    train_soft_neg_acc.update(
                        anchor_text_emb, 
                        pos_code_emb, neg_code_emb
                    )
                    batch_loss = self.loss_fn(anchor_text_emb, pos_code_emb, neg_code_emb).mean()
                    pbar.set_description(f"train: epoch: {epoch_i+1}/{epochs} batch_loss: {batch_loss:.3f} loss: {np.mean(batch_losses):.3f} acc: {100*train_soft_neg_acc.get():.2f}")
                batch_loss.backward()
                self.optimizer.step()
                # scheduler.step()  # Update learning rate schedule
                self.zero_grad()
                batch_losses.append(batch_loss.item())
                # if step == 5: break # DEBUG
                if ((step+1) % VALID_STEPS == 0) or ((step+1) == len(trainloader)):
                    # validate current model
                    print(rule_wise_acc())
                    print(dict(rule_wise_acc.counts))
                    # if intent_level_dynamic_sampling or use_AST or self.code_retriever_baseline:
                    #     s = time.time()
                    #     val_acc = self.val_ret(valset, device=device)
                    #     print(f"validated in {time.time()-s}s")
                    #     print(f"recall@5 = {100*val_acc:.3f}")
                    #     val_loss = None
                    # else:        
                    #     val_acc, val_loss = self.val(valloader, epoch_i=epoch_i, 
                    #                                  epochs=epochs, device=device)
                    
                    s = time.time()
                    val_acc = self.val_ret(valset, device=device)
                    print(f"validated in {time.time()-s}s")
                    print(f"recall@5 = {100*val_acc:.3f}")
                    val_loss = None

                    # save model only after warmup is complete.
                    if val_acc > best_val_acc and (not(hasattr(trainset, "warmup_steps")) or trainset.warmup_steps == 0):
                        print(f"saving best model till now with val_acc: {val_acc} at {save_path}")
                        best_val_acc = val_acc
                        torch.save(self.state_dict(), save_path)

                    train_metrics["log_steps"].append({
                        "train_batch_losses": batch_losses, 
                        "train_loss": np.mean(batch_losses), 
                        "val_loss": val_loss,
                        "val_acc": 100*val_acc,
                    })
                    if (self.use_cross_entropy or self.code_retriever_baseline):
                        train_metrics["train_acc"] = 100*train_acc/train_tot
                        if self.code_retriever_baseline:
                            train_metrics["train_u_acc"] = 100*train_u_acc/train_tot
                    else:
                        train_metrics["train_soft_neg_acc"] = 100*train_soft_neg_acc.get()
                        train_metrics["train_hard_neg_acc"] = 100*train_hard_neg_acc.get()
                    metrics_path = os.path.join(exp_name, "train_metrics.json")
                    print(f"saving metrics to {metrics_path}")
                    with open(metrics_path, "w") as f:
                        json.dump(train_metrics, f)
            if self.code_retriever_baseline: trainset.reset()        
        
        return train_metrics

    
def main(args):    
    print("initializing model and tokenizer ..")
    tok_path = get_tok_path("graphcodebert")
    print("creating model object")
    triplet_net = GraphCodeBERTripletNet(tok_path=tok_path, **vars(args))
    if args.train:
        print("commencing training")
        if args.disco_baseline:
            metrics = fit_disco(triplet_net, model_name="graphcodebert", **vars(args))
        else: metrics = triplet_net.fit(**vars(args))
        metrics_path = os.path.join(args.exp_name, "train_metrics.json")
        print(f"saving metrics to {metrics_path}")
        with open(metrics_path, "w") as f:
            json.dump(metrics, f)
    if args.predict:
        model_path = os.path.join(args.exp_name, "model.pt")
        print(model_path)
        
def test_retreival(args):
    print("initializing model and tokenizer ..")
    tok_path = os.path.join(os.path.expanduser("~"), "graphcodebert-base-tok")
    device = args.device_id if torch.cuda.is_available() else "cpu"
    
    ckpt_path = os.path.join(args.exp_name, "model.pt")
    print(f"loading checkpoint (state dict) from {ckpt_path}")
    try: state_dict = torch.load(ckpt_path, map_location="cpu")
    except Exception as e: 
        state_dict = None
        print("\x1b[31;1mCouldn't load state dict because\x1b[0m")
        print(e)
    
    print("creating model object")
    triplet_net = GraphCodeBERTripletNet(tok_path=tok_path)
    if state_dict: 
        print(f"\x1b[32;1msuccesfully loaded state dict from {ckpt_path}\x1b[0m")
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
                                               use_tqdm=True, **vars(args))
            query_mat = torch.stack(query_mat)

            print(f"encoding {len(candidates)} candidates:")
            if setting == "code":
                cand_mat = triplet_net.encode_emb(candidates, mode="code", 
                                                  use_tqdm=True, **vars(args))
                cand_mat = torch.stack(cand_mat)
            elif setting == "annot":
                cand_mat = triplet_net.encode_emb(candidates, mode="text", 
                                                  use_tqdm=True, **vars(args))
                cand_mat = torch.stack(cand_mat)
            else:
                cand_mat_code = triplet_net.encode_emb(code_candidates, mode="code", 
                                                       use_tqdm=True, **vars(args))
                cand_mat_annot = triplet_net.encode_emb(annot_candidates, mode="text", 
                                                        use_tqdm=True, **vars(args))
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
                    "GraphCodeBERT_zero_shot", 
                    f"test_metrics_{dist_func}_{setting}.json"
                )
                os.makedirs("GraphCodeBERT_zero_shot", exist_ok=True)
            with open(metrics_path, "w") as f:
                json.dump(metrics, f)
#     with open("pred_cand_ranks.json", "w") as f:
#         json.dump(label_ranks, f, indent=4)
if __name__ == "__main__":
    args = get_args()
    if args.train:
        main(args=args) 
    elif args.predict:
        test_retreival(args=args)
    if args.test_ood: 
        print("creating model object")
        # instantiate model class.
        tok_path = get_tok_path("graphcodebert")
        triplet_net = GraphCodeBERTripletNet(tok_path=tok_path, **vars(args))
        test_ood_performance(
            triplet_net, model_name="graphcodebert", args=args,
            query_paths=["query_and_candidates.json", "external_knowledge/queries.json", 
                         "data/queries_webquery.json", "data/queries_codesearchnet.json"],
            cand_paths=["candidate_snippets.json", "external_knowledge/candidates.json", 
                        "data/candidates_webquery.json", "data/candidates_codesearchnet.json"], 
        )