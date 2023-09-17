# **SYNC**
Implementation for SYNC: Structurally guided Hard Negative Curricula for Generalizable Neural Code Search

## Environment
To recreate our computational environment we provide a YML file export of our conda environment (py3.7.yml)
```
conda env create -f py3.7.yml
```

## **Data:**
You can download all the files from here: https://drive.google.com/drive/folders/1LFm1lrjKxYHAm9Dq7fKx8m0ZNtRv88wI?usp=sharing 
(put in data folder).

and 

`triple` files from here: https://drive.google.com/drive/folders/18796d0_F9hpv6sV7-2RBQA4DMMVxLlVL?usp=sharing (put in triples folder).

Download the pre-generated negative samples for SYNC () and DISCO ().

## **Running Instructions:**
Running instructions for training models and baselines. <br>
(**NOTE:** for the ```<model_name>_ood.sh``` scripts just adjust the experiment folder path or ```-en``` argument)

### **Baselines:**
Simple baselines:

**nBOW:**
```
bash scripts/train_nbow.sh
bash scripts/nbow_ood.sh
```

**RNN:**
```
bash scripts/train_rnn.sh
bash scripts/rnn_ood.sh
```

**CNN:**
```
bash scripts/train_cnn.sh
bash scripts/cnn_ood.sh
```

**Regular triplet training:**

CodeBERT:
```
```
GraphCodeBERT:
```
```
UniXcoder:
```
```

**DNS:**

CodeBERT:
```
```
GraphCodeBERT:
```
```
UniXcoder:
```
```

**DISCO:**

CodeBERT:
```
bash scripts/codebert_disco.sh
bash scripts/codebert_ood.sh
```
GraphCodeBERT:
```
```
UniXcoder:
```
```

CodeRetriever:

CodeBERT:
```
bash scripts/codebert_code_retriever.sh
bash scripts/codebert_ood.sh
```
GraphCodeBERT:
```
```
UniXcoder:
```
```

### **SYNC experiments:**
```

## **Citation:**
```
@inproceedings{zhou23docprompting,
    title = {SYNC: A Structurally guided Hard Negative Curricula for
Generalizable Neural Code Search},
    author = {Atharva Naik and Soumtira Das and Jyothi Vedurada and Somak Aditya},
    booktitle = {The Asia-Pacific Chapter of the Association for Computational Linguistics},
    month = {November},
    year = {2023}
}
```