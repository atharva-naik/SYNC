python -m models.CodeBERT -t -tp data/conala-mined-100k_train.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_disco -d "cuda:1" -bs 48 -e 5 -disco -pcp "CoNaLa_AST_neg_samples_n1.json"