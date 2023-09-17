# **SYNC**
Implementation for SYNC: Structurally guided Hard Negative Curricula for Generalizable Neural Code Search

## Environment
To recreate our computational environment we provide a YML file export of our conda environment (py3.7.yml)
```
conda env create -f py3.7.yml
```

## **Data:**
You can download all the files from here: https://drive.google.com/drive/folders/1LFm1lrjKxYHAm9Dq7fKx8m0ZNtRv88wI?usp=sharing 

## **Running Instructions:**
Running instructions for training models and baselines.

### **Baselines:**
Simple baselines:

nBOW:
```
bash scripts/train_nbow.sh
bash scripts/predict_nbow.sh
```

RNN:
```
bash scripts/
```

CNN:
```
```

### **SYNC experiments:**

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