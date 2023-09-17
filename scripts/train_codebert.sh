python -m models.CodeBERT -t -tp data/conala-mined-100k_train_triplets.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_ast_1_100k -d "cuda:0" -bs 48 -e 2 -beta 0.0001 -p 2 -ast -pcp "CoNaLa_AST_neg_samples.json"
# python -m models.CodeBERT -t -tp data/conala-mined-100k_train.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_intent_dyn_neg_sample_100k -d "cuda:0" -bs 48 -e 3 -idns -sip "CoNaLa_top10_sim_intents.json" 
python -m models.CodeBERT -t -tp data/conala-mined-100k_train_triplets.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_ast_1_100k -d "cuda:0" -bs 48 -e 2 -beta 0.0001 -p 2 -sct -ast -pcp "CoNaLa_AST_neg_samples.json"
python -m models.CodeBERT -t -tp data/conala-mined-100k_train_triplets.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_100k_dup -d "cuda:0" -bs 48 -e 5
python -m models.CodeBERT -t -tp data/conala-mined-100k_train_triplets.json -vp data/conala-mined-100k_val.json -en experiments/CodeBERT_ast_18_100k_dup -d "cuda:0" -bs 48 -e 5 -beta 0.01 -p 2 -ast -pcp "CoNaLa_AST_neg_samples.json" -w 17000