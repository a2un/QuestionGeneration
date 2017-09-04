import argparse
import json
import math
import os
import shutil
from pprint import pprint
from helpers import utils

import tensorflow as tf
from tqdm import tqdm
import numpy as np

from basic.evaluator import F1Evaluator, Evaluator, ForwardEvaluator, MultiGPUF1Evaluator
from basic.graph_handler import GraphHandler
from basic.model import Model, get_multi_gpu_models
#from basic_old.model_margin import Model, get_multi_gpu_models
from basic.trainer import Trainer, MultiGPUTrainer
from basic.read_data import load_metadata, read_data, get_squad_data_filter, update_config

def main(config):
    # Get gpu masks of the form 0,1,2
    gpu_masks = ""
    for i in range(0, config.num_gpus):
        gpu_masks += "," + str(config.gpu_idx + i)
    gpu_masks = gpu_masks[1:]

    import os
    os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
    os.environ["CUDA_VISIBLE_DEVICES"]=gpu_masks
    set_dirs(config)
    set_dirs(config)
    with tf.device(config.device):
        if config.mode == 'train':
            _train(config)
        elif config.mode == 'test':
            _test(config)
        elif config.mode == 'forward':
            _forward(config)
        else:
            raise ValueError("invalid value for 'mode': {}".format(config.mode))


def _config_draft(config):
    if config.draft:
        config.num_steps = 2
        config.eval_period = 1
        config.log_period = 1
        config.save_period = 1
        config.eval_num_batches = 2

def save_batch_results(batch_list, save_dir='results', f1_thres=0.1):
    q = []
    answerss = []
    x = []
    predicted_answers = []
    best_answers = []
    best_f1_scores = []
    predicted_f1_scores = []

    total_best_f1_scores = []
    total_predicted_f1_scores = []
    for batch in batch_list:
        total_best_f1_scores.extend(batch['best_f1_scores'])
        total_predicted_f1_scores.extend(batch['predicted_f1_scores'])
        for i in range(0, len(batch['answerss'])):
            if batch['best_f1_scores'][i] - batch['predicted_f1_scores'][i] > f1_thres:
                x.append((' '.join(batch['x'][i][0])).rstrip('\r\n'))
                predicted_answers.append(batch['predicted_answers'][i])
                best_answers.append(batch['best_answers'][i])
                best_f1_scores.append(str(batch['best_f1_scores'][i]))
                predicted_f1_scores.append(str(batch['predicted_f1_scores'][i]))
                answerss.append(list(map(lambda ans: ans.rstrip('\r\n'), batch['answerss'][i])))
                q.append(batch['q'][i])
    avg_best_f1_scores = np.mean(total_best_f1_scores)
    avg_predicted_f1_scores = np.mean(total_predicted_f1_scores)

    score_msg = ["PREDICTED %s BEST %s NUM_SAMPLES %s NUM_TOTAL %s" % \
    (avg_predicted_f1_scores, avg_best_f1_scores, \
        len(best_answers), len(total_best_f1_scores))]
    utils.save_lines(score_msg, "%s/f1_score_comparison.txt" % save_dir)
    utils.save_lines(best_f1_scores, "%s/best_f1_scores.txt" % save_dir)
    utils.save_lines(predicted_f1_scores, "%s/predicted_f1_scores.txt" % save_dir)
    utils.save_lines(best_answers, "%s/best_answers.txt" % save_dir)
    utils.save_lines(predicted_answers, "%s/predicted_answers.txt" % save_dir)
    utils.save_lines(x, "%s/paragraphs.txt" % save_dir)
    utils.save_tabbed_lines(answerss, "%s/gold_answers.txt" % save_dir)
    utils.save_tabbed_lines(q, "%s/questions.txt" % save_dir)

def _train(config):
    # load_metadata(config, 'train')  # this updates the config file according to metadata file
    k=config.k
    sup_unsup_ratio=config.sup_unsup_ratio
    save_dir = 'error_results_newsqa_k=%s' % k
    f1_thres = 0.1

    data_filter = get_squad_data_filter(config)
    train_data = read_data(config, 'train', config.load, data_filter=data_filter)
    dev_data = read_data(config, 'dev', True, data_filter=data_filter)

    # Baseline model
    config.data_dir = config.baseline_dir
    squad_train_data = read_data(config, 'train', config.load, data_filter=data_filter)

    # test_data = read_data(config, 'test', True, data_filter=data_filter)
    update_config(config, [squad_train_data, train_data, dev_data])

    _config_draft(config)

    word2vec_dict = train_data.shared['lower_word2vec'] if config.lower_word else train_data.shared['word2vec']
    word2idx_dict = train_data.shared['word2idx']
    idx2vec_dict = {word2idx_dict[word]: vec for word, vec in word2vec_dict.items() if word in word2idx_dict}
    print("{}/{} unique words have corresponding glove vectors.".format(len(idx2vec_dict), len(word2idx_dict)))
    print(len(word2vec_dict))

    emb_mat = np.array([idx2vec_dict[idx] if idx in idx2vec_dict
                        else np.random.multivariate_normal(np.zeros(config.word_emb_size), np.eye(config.word_emb_size))
                        for idx in range(config.word_vocab_size)])
    config.emb_mat = emb_mat

    # construct model graph and variables (using default graph)
    pprint(config.__flags, indent=2)
    # model = Model(config)
    models = get_multi_gpu_models(config)
    model = models[0]
    trainer = MultiGPUTrainer(config, models)
    evaluator = MultiGPUF1Evaluator(config, models, tensor_dict=model.tensor_dict if config.vis else None)
    graph_handler = GraphHandler(config)  # controls all tensors and variables in the graph, including loading /saving

    # Variables
    config_proto = tf.ConfigProto(allow_soft_placement=True)
    config_proto.gpu_options.per_process_gpu_memory_fraction = 0.65

    sess = tf.Session(config=config_proto)
    graph_handler.initialize(sess)

    batches_list = []

    # begin training
    num_steps = config.num_steps or int(math.ceil(train_data.num_examples / (config.batch_size * config.num_gpus))) * config.num_epochs
    global_step = 0
    global_scores = []

    # Combine batching together
    train_data_batcher = train_data.get_multi_batches(config.batch_size, config.num_gpus,
                                                     num_steps=num_steps, shuffle=True, cluster=config.cluster)
    squad_data_batcher = squad_train_data.get_multi_batches(config.batch_size, config.num_gpus,
                                                     num_steps=num_steps, shuffle=True, cluster=config.cluster)
    idx = [-1]
    ratio = sup_unsup_ratio

    def combine_batchers(unsupervised_generator,
        supervised_generator, ratio):
        while True:
            idx[0] = idx[0] + 1
            if idx[0] % ratio == 0: 
                print("Yielding unsupervised")
                unsup_batch = next(unsupervised_generator)
                for _, data_set in unsup_batch:
                    if config.use_special_token:
                        data_set.data['dataset_type'] = ['NEWSQA']

                y = data_set.data['y']
                x = data_set.data['x']
                q = data_set.data['q']
                for xi, yi, qi in zip(x, y, q):
                    start_id = yi[0][0][1]
                    end_id = yi[0][1][1]
                    ans = xi[0][start_id:end_id]
                yield unsup_batch            
            else:
                print("Yielding squad")
                sup_batch = next(supervised_generator)
                y = data_set.data['y']
                x = data_set.data['x']
                for xi, yi in zip(x, y):
                    start_id = yi[0][0][1]
                    end_id = yi[0][1][1]

                    ans = xi[0][start_id:end_id]
                yield sup_batch 

    combined_batcher = combine_batchers(train_data_batcher, squad_data_batcher, ratio=ratio)

    for batches in tqdm(combined_batcher, total=num_steps):

        global_step = sess.run(model.global_step) + 1  # +1 because all calculations are done after step
        get_summary = global_step % config.log_period == 0

        scores = trainer.get_scores(sess, batches, get_summary=get_summary, k=k)
        loss, summary, train_op = trainer.margin_step(sess, batches=batches, top_k_batches=scores, get_summary=get_summary)
        #loss, summary, train_op = trainer.step(sess, batches=batches, get_summary=get_summary)
        if get_summary:
            graph_handler.add_summary(summary, global_step)

        # occasional saving
        if global_step % config.save_period == 0:
            graph_handler.save(sess, global_step=global_step)

        if not config.eval:
            continue
        # Occasional evaluation
        if global_step % config.eval_period == 0:
            num_steps = math.ceil(dev_data.num_examples / (config.batch_size * config.num_gpus))
            if 0 < config.eval_num_batches < num_steps:
                num_steps = config.eval_num_batches
            e_train = evaluator.get_evaluation_from_batches(
                sess, tqdm(train_data.get_multi_batches(config.batch_size, config.num_gpus, num_steps=num_steps), total=num_steps)
            )
            graph_handler.add_summaries(e_train.summaries, global_step)
            e_dev = evaluator.get_evaluation_from_batches(
                sess, tqdm(dev_data.get_multi_batches(config.batch_size, config.num_gpus, num_steps=num_steps), total=num_steps))
            graph_handler.add_summaries(e_dev.summaries, global_step)

            if config.dump_eval:
                graph_handler.dump_eval(e_dev)
            if config.dump_answer:
                graph_handler.dump_answer(e_dev)


def _test(config):
    assert config.load
    test_data = read_data(config, 'test', True)#read_data(config, 'test', True)
    update_config(config, [test_data])

    _config_draft(config)

    if config.use_glove_for_unk:
        word2vec_dict = test_data.shared['lower_word2vec'] if config.lower_word else test_data.shared['word2vec']
        new_word2idx_dict = test_data.shared['new_word2idx']
        idx2vec_dict = {idx: word2vec_dict[word] for word, idx in new_word2idx_dict.items()}
        # print("{}/{} unique words have corresponding glove vectors.".format(len(idx2vec_dict), len(word2idx_dict)))
        new_emb_mat = np.array([idx2vec_dict[idx] for idx in range(len(idx2vec_dict))], dtype='float32')
        config.new_emb_mat = new_emb_mat

    pprint(config.__flags, indent=2)
    models = get_multi_gpu_models(config)
    evaluator = MultiGPUF1Evaluator(config, models, tensor_dict=models[0].tensor_dict if config.vis else None)
    graph_handler = GraphHandler(config)  # controls all tensors and variables in the graph, including loading /saving

    config_proto = tf.ConfigProto(allow_soft_placement=True)
    config_proto.gpu_options.per_process_gpu_memory_fraction = 0.7

    sess = tf.Session(config=config_proto)
    graph_handler.initialize(sess)
    num_steps = math.ceil(test_data.num_examples / (config.batch_size * config.num_gpus))
   

    #if 0 < config.eval_num_batches < num_steps:
    #    num_steps = config.eval_num_batches

   
    #print(config.eval_num_batches)
    #assert(False)
    e = None
    for multi_batch in tqdm(test_data.get_multi_batches(config.batch_size, config.num_gpus, num_steps=num_steps, cluster=config.cluster), total=num_steps):
        idx, batch = multi_batch[0]
        if config.use_special_token:
            batch.data['dataset_type'] = ['NEWSQA']
        ei = evaluator.get_evaluation(sess, multi_batch)
        e = ei if e is None else e + ei
        if config.vis:
            eval_subdir = os.path.join(config.eval_dir, "{}-{}".format(ei.data_type, str(ei.global_step).zfill(6)))
            if not os.path.exists(eval_subdir):
                os.mkdir(eval_subdir)
            path = os.path.join(eval_subdir, str(ei.idxs[0]).zfill(8))
            graph_handler.dump_eval(ei, path=path)
    print(e)
    if config.dump_answer:
        print("dumping answer ...")
        graph_handler.dump_answer(e)
    if config.dump_eval:
        print("dumping eval ...")
        graph_handler.dump_eval(e)


def _forward(config):
    assert config.load
    test_data = read_data(config, config.forward_name, True)
    update_config(config, [test_data])

    _config_draft(config)

    if config.use_glove_for_unk:
        word2vec_dict = test_data.shared['lower_word2vec'] if config.lower_word else test_data.shared['word2vec']
        new_word2idx_dict = test_data.shared['new_word2idx']
        idx2vec_dict = {idx: word2vec_dict[word] for word, idx in new_word2idx_dict.items()}
        # print("{}/{} unique words have corresponding glove vectors.".format(len(idx2vec_dict), len(word2idx_dict)))
        new_emb_mat = np.array([idx2vec_dict[idx] for idx in range(len(idx2vec_dict))], dtype='float32')
        config.new_emb_mat = new_emb_mat

    pprint(config.__flags, indent=2)
    models = get_multi_gpu_models(config)
    model = models[0]
    evaluator = ForwardEvaluator(config, model)
    graph_handler = GraphHandler(config)  # controls all tensors and variables in the graph, including loading /saving

    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
    graph_handler.initialize(sess)

    num_batches = math.ceil(test_data.num_examples / config.batch_size)
    if 0 < config.eval_num_batches < num_batches:
        num_batches = config.eval_num_batches
    e = evaluator.get_evaluation_from_batches(sess, tqdm(test_data.get_batches(config.batch_size, num_batches=num_batches), total=num_batches))
    print(e)
    if config.dump_answer:
        print("dumping answer ...")
        graph_handler.dump_answer(e, path=config.answer_path)
    if config.dump_eval:
        print("dumping eval ...")
        graph_handler.dump_eval(e)


def set_dirs(config):
    # create directories
    if not config.load and os.path.exists(config.out_dir):
        shutil.rmtree(config.out_dir)

    config.save_dir = os.path.join(config.out_dir, "save")
    config.log_dir = os.path.join(config.out_dir, "log")
    config.eval_dir = os.path.join(config.out_dir, "eval")
    config.answer_dir = os.path.join(config.out_dir, "answer")
    if not os.path.exists(config.out_dir):
        os.makedirs(config.out_dir)
    if not os.path.exists(config.save_dir):
        os.mkdir(config.save_dir)
    if not os.path.exists(config.log_dir):
        os.mkdir(config.log_dir)
    if not os.path.exists(config.answer_dir):
        os.mkdir(config.answer_dir)
    if not os.path.exists(config.eval_dir):
        os.mkdir(config.eval_dir)


def _get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_path")
    return parser.parse_args()


class Config(object):
    def __init__(self, **entries):
        self.__dict__.update(entries)


def _run():
    args = _get_args()
    with open(args.config_path, 'r') as fh:
        config = Config(**json.load(fh))
        main(config)


if __name__ == "__main__":
    _run()