import os
import json
import matplotlib
import numpy as np
import matplotlib.pyplot as plt

def process(y: float, check_val=0.8, transform_val: float=1) -> float:
    if np.round(y,3) == check_val:
        return transform_val
    return y

def downsample_array(array, k=500):
    new_array = []
    for i in range(len(array)):
        if i % k == 0:
            new_array.append(array[i])

    return np.array(new_array)

def process_array(array):
    for i in range(len(array)):
        array[i] = process(array[i])
    return array

# main
if __name__ == "__main__":
    codebert_train_metrics = json.load(open("./experiments/CodeBERT_100k_dup/train_metrics.json"))
    codebert_SYNC_train_metrics = json.load(open("./experiments/CodeBERT_ast_18_100k_dup/train_metrics.json"))
    base_recall = [i["val_acc"] for i in codebert_train_metrics["log_steps"]]
    SYNC_recall = [i["val_acc"] for i in codebert_SYNC_train_metrics["log_steps"]]
    # print(base_recall)
    # print(SYNC_recall)
    fig, ax = plt.subplots()
    ax.set_title("CodeBERT vs CodeBERT+SYNC")
    x = range(1, len(base_recall)+1)
    N = len(codebert_SYNC_train_metrics["log_steps"][0]["soft_neg_weights"])
    # print(N)
    soft_weights = np.array([np.mean(process_array(np.array(i["soft_neg_weights"]))) for i in codebert_SYNC_train_metrics["log_steps"]]).flatten()
    soft_weights_x = np.array([np.mean(np.array(range(N))/N+i) for i in x]).flatten()
    # soft_weights = np.array([downsample_array(process_array(14*np.array(i["soft_neg_weights"]))) for i in codebert_SYNC_train_metrics["log_steps"]]).flatten()
    # soft_weights_x = np.array([downsample_array(np.array(range(N))/N+i) for i in x]).flatten()
    assert len(soft_weights_x) == len(soft_weights)
    # print(soft_weights)
    ax.plot(x, base_recall, label="CodeBERT")
    # plt.fill_between(x=x, y1=base_recall, alpha=0.3)
    ax.plot(x, SYNC_recall, label="CodeBERT+SYNC")
    ax2 = ax.twinx()
    ax2.plot(soft_weights_x, soft_weights, label="CodeBERT+SYNC\nsoft -ve attn", alpha=0.7, color="green")
    # plt.fill_between(x=soft_weights_x, y1=soft_weights, alpha=0.4, color="green")
    ax.legend(loc="lower left")
    ax2.legend(loc="lower right")
    ax.set_xlabel("log steps")
    ax.set_ylabel("val recall@5")
    xticks = [0,10,20,30,40,50]
    ax.set_xticks(xticks, labels=xticks)
    yticks = [0.6,0.7,0.8,0.9,1]
    ax2.set_yticks(yticks, labels=yticks)
    fig.savefig("./plots/soft_attn_and_val_recall.png")