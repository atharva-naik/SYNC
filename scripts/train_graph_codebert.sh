# python -m models.GraphCodeBERT -t -tp triples/triples_codesearchnet_fixed_train.json -vp triples/triples_codesearchnet_fixed_val.json -en experiments/GraphCodeBERT_codesearchnet_dyn_neg_sample --device_id "cuda:1" -bs 32 -e 1 -dns
# python -m models.GraphCodeBERT -t -tp data/conala-mined-100k_train_triplets.json -vp data/conala-mined-100k_val.json -en experiments/GraphCodeBERT_ast_1_100k -d "cuda:1" -bs 32 -e 2 -beta 0.01 -p 2 -ast -pcp "CoNaLa_AST_neg_samples.json" -w 5000
python -m models.GraphCodeBERT -t -tp data/conala-mined-100k_train_triples_10.json -vp data/conala-mined-100k_val.json -en experiments/GraphCodeBERT_100k_10neg -d "cuda:0" -bs 32 -e 5