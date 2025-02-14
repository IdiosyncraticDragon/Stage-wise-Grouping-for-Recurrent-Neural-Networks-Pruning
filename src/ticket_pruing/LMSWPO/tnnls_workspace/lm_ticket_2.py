#encoding=utf-8
'''
Author: Guiying li, Yanglong yu
contact: ligy@sustech.edu.cn
'''

import sys
# read parameters
CUDA_VISIBLE_DEVICES = 0
name_mark = 'lm_iterative_tipo'
GPU_ID = 0
layer_group_type = 'layer'
Model_type = 'LM'
acc_percent_prune = 0.01
other_GPU_IDs = [1]
HOME_PATH = '/fl'
DATA_PATH = '/fl'
MODEL_PATH = '/fl'
#reload(sys)
#sys.setdefaultencoding('utf-8')
sys.path.insert(0, '{}/LMSWPO/'.format(HOME_PATH))
sys.path.insert(0, '{}/LMSWPO/tnnls_workspace/package/'.format(HOME_PATH))
sys.path.insert(0, HOME_PATH+'/')



from logger import Logger
import os
import torch
from torch.multiprocessing import Process
if __name__ == "__main__":
    torch.multiprocessing.set_start_method('spawn')

import torch.nn as nn
from torch.autograd import Variable
from torch import cuda
import torch.distributed as dist
#import ncs
from onmt.Utils import use_gpu
import onmt
import bleu
import math
import time
import ncs
import numpy as np
import os,os.path,datetime
from masked_networkLM import MaskedModel
import copy

# for language model
import data


if Model_type == 'LuongNet':
    model_index = -2
    if layer_group_type == 'simple':
        from layer_group_luong_simple import group_dict #group_dict1, group_dict2
    elif layer_group_type == 'time':
        from layer_group_luong_time import group_dict
    elif layer_group_type == 'layer':
        from layer_group_luong_layer import group_dict
    else:
        print("Please input correct layer group type !!!!")
        exit()
elif Model_type == 'RNNSearch':
    model_index = -3
    if layer_group_type == 'simple':
        from layer_group_RNN_simple import group_dict
    elif layer_group_type == 'time':
        from layer_group_RNN_time import group_dict
    elif layer_group_type == 'layer':
        from layer_group_RNN_layer import group_dict
    else:
        print("Please input correct layer group type !!!!")
        exit()
elif Model_type == 'LM':
    model_index = -1
    if layer_group_type == 'simple':
        from layer_group_lm_simple import group_dict
    elif layer_group_type == 'time':
        from layer_group_lm_time import group_dict
    elif layer_group_type == 'layer':
        from layer_group_lm_layer import group_dict
    else:
        print("Please input correct layer group type !!!!")
        exit()
else:
    print("Please input correct Model type !!!!")
    exit()

logger_path = '{}/test/{}{}_lmrnn-logs'.format(HOME_PATH, name_mark, acc_percent_prune )
logger = Logger(logger_path)

if Model_type == 'RNNSearch':
    LEARNING_RATE = 1.0
elif Model_type == 'LuongNet':
    LEARNING_RATE = 1.0


SAVE_MODEL_PATH = './iterative_retrain_{}'.format(name_mark)
SAVE_MODEL_FOLDER = '{}/checkpoint/language-mode/prune/'.format(MODEL_PATH)
SAVE_MODEL_TMP_FOLDER ='{}/checkpoint/language-mode/prune_tmp/'.format(MODEL_PATH)


REPORT_EVERY = 5000
EPOCHS = 1
MAX_GRAD_NORM = 1
START_DECAY_AT = 8
TRAIN_BATCH_SIZE= 20
TEST_BATCH_SIZE= 10
RETRAIN_EPOCHS = 6
# EVLAUTE METRIC
EVAL_FUNC = nn.CrossEntropyLoss()
SEQ_LEN = 35
GRAD_CLIP = 0.25
TRAIN_LOG_INTERVAL = 200
LR_INIT = 20
LR = LR_INIT

def time_now():
    now = int(round(time.time()*1000))
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now/1000))


#----------------from OpenNMT-py----------------------------------


def fix_no_leaf(model, pretrained_leaf_dict, prefix=''):
    for name, param in model._parameters.items():
        param_name = prefix + ('.' if prefix else '') + name
        if not param_name in pretrained_leaf_dict.keys():
            continue
        if param is not None and pretrained_leaf_dict[param_name] and not param.is_leaf:
            print(param_name)
            model._parameters[name] = Variable(param.data, requires_grad = True)
    for mname, module in model._modules.items():
        if module is not None:
            submodule_prefix = prefix + ('.' if prefix else '') + mname
            fix_no_leaf(module, pretrained_leaf_dict, prefix=submodule_prefix)

def build_optim(model, checkpoint, opt, pretrained_leaf_dict):
    if opt.train_from:
        print('Loading optimizer from checkpoint.')
        optim = checkpoint['optim']
        optim.optimizer.load_state_dict(
            checkpoint['optim'].optimizer.state_dict())
    else:
        # what members of opt does Optim need?
        optim = onmt.Optim(
            opt.optim, opt.learning_rate, opt.max_grad_norm,
            lr_decay=opt.learning_rate_decay,
            start_decay_at=opt.start_decay_at,
            opt=opt
        )
    fix_no_leaf(model, pretrained_leaf_dict)
    
    for name, module_tensor in model.named_parameters(): 
        if not module_tensor.is_leaf:
            print("name:", name, " still need repair")
    optim.set_parameters(model.parameters())

    return optim

def make_train_data_iter(train_data, opt):
    """
    This returns user-defined train data iterator for the trainer
    to iterate over during each train epoch. We implement simple
    ordered iterator strategy here, but more sophisticated strategy
    like curriculum learning is ok too.
    """
    return onmt.IO.OrderedIterator(
                dataset=train_data, batch_size=opt.batch_size,
                device=opt.gpuid[0] if opt.gpuid else -1,
                repeat=False)

def make_valid_data_iter(valid_data, batch_size=TEST_BATCH_SIZE, gpu_id=None):
    """
    This returns user-defined validate data iterator for the trainer
    to iterate over during each validate epoch. We implement simple
    ordered iterator strategy here, but more sophisticated strategy
    is ok too.
    """
    return onmt.IO.OrderedIterator(
                dataset=valid_data, batch_size= batch_size,
                device= gpu_id if gpu_id is not None else GPU_ID,
                train=False, sort=True)

def make_loss_compute(model, tgt_vocab, dataset, gpu_id=None, copy_attn=False, copy_attn_force=False):
    """
    This returns user-defined LossCompute object, which is used to
    compute loss in train/validate process. You can implement your
    own *LossCompute class, by subclassing LossComputeBase.
    """
    if copy_attn:
        compute = onmt.modules.CopyGeneratorLossCompute(
            model.generator, tgt_vocab, dataset, copy_attn_force)
    else:
        compute = onmt.Loss.NMTLossCompute(model.generator, tgt_vocab)

    if gpu_id == None:
        gpu_id = cuda.current_device()
    compute.cuda(gpu_id)

    return compute

def report_func(epoch, batch, num_batches,
                start_time, lr, report_stats):
    """
    This is the user-defined batch-level traing progress
    report function.
    Args:
        epoch(int): current epoch count.
        batch(int): current batch count.
        num_batches(int): total number of batches.
        start_time(float): last report time.
        lr(float): current learning rate.
        report_stats(Statistics): old Statistics instance.
    Returns:
        report_stats(Statistics): updated Statistics instance.
    """
    if batch % REPORT_EVERY == -1 % REPORT_EVERY:
        report_stats.output(epoch, batch+1, num_batches, start_time)
        report_stats = onmt.Statistics()

    return report_stats

def train_model(model, train_data, valid_data, fields, optim, opt, num_runs, stop_acc):
    '''
    input: stop_acc, means stop in stop_acc
    output:
        recorverd, if successfully recorverd accuracy, return True
    '''
    recovered = False
    train_iter = make_train_data_iter(train_data, opt)
    # 两者接口不同
    valid_iter = make_valid_data_iter(valid_data, TEST_BATCH_SIZE)

    train_loss = make_loss_compute(model, fields["tgt"].vocab,
                                   train_data)
    valid_loss = make_loss_compute(model, fields["tgt"].vocab,
                                   valid_data)

    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches

    trainer = onmt.Trainer(model, train_iter, valid_iter,
                           train_loss, valid_loss, optim,
                           trunc_size, shard_size)
    # pdb.set_trace()
    # print(opt.start_epoch, opt.epochs)
    # print(opt.batch_size)
    # valid_stats = trainer.validate()
    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print(time_now(), "epoch:", epoch)

        # 1. Train for one epoch on the training set.
        train_stats = trainer.train(epoch, report_func)
        print('Train perplexity: %g' % train_stats.ppl())
        print('Train accuracy: %g' % train_stats.accuracy())
        logger.scalar_summary('train_%s_ppl' % num_runs, train_stats.ppl(), num_runs*(opt.epochs+1)+epoch)
        logger.scalar_summary('train_%s_acc' % num_runs, train_stats.accuracy(), num_runs*(opt.epochs+1)+epoch)

        # 2. Validate on the validation set.
        valid_stats = trainer.validate()
        print('Validation perplexity: %g' % valid_stats.ppl())
        print('Validation accuracy: %g' % valid_stats.accuracy())

        logger.scalar_summary('train_%s_val_ppl' % num_runs, valid_stats.ppl(), num_runs*(opt.epochs+1)+epoch)
        logger.scalar_summary('train_%s_val_acc' % num_runs, valid_stats.accuracy(), num_runs*(opt.epochs+1)+epoch)

        # 3. Log to remote server.
        # if opt.exp_host:
        #     train_stats.log("train", experiment, optim.lr)
        #     valid_stats.log("valid", experiment, optim.lr)

        # 4. Update the learning rate
        trainer.epoch_step(valid_stats.ppl(), epoch)

        if valid_stats.accuracy() >= stop_acc:
            print("epoch %s recovered accuracy %s and new valid accuracy %s" % (epoch, stop_acc, valid_stats.accuracy()))
            return True
        # 5. Drop a checkpoint if needed.
        # if epoch >= opt.start_checkpoint_at:
        #     trainer.drop_checkpoint(opt, epoch, fields, valid_stats)
    return recovered
#----------------end OpenNMT-py----------------------------------

def translate_opt_initialize(trans_p, trans_dum_p):
    translate_opt = torch.load(trans_p)
    translate_dummy_opt = torch.load(trans_dum_p)
    #   translate
    translate_opt.model = weights
    #   dataset for pruning
    #translate_opt.src = '/home/lgy/data/wmt/wmt17-en-zh/smalltrain-test-en.txt.tok'
    #translate_opt.tgt = '/home/lgy/data/wmt/wmt17-en-zh/smalltrain-test-zh.txt.tok'
    translate_opt.src = '{}/data/wmt/wmt14/opennmtdata/iterative/origin/en-test.txt'.format(cfg.DATA_PATH)
    translate_opt.tgt = '{}/data/wmt/wmt14/opennmtdata/iterative/origin/de-test.txt'.format(cfg.DATA_PATH)
    translate_opt.start_epoch = 2
    translate_opt.model = weights
    translate_opt.gpu = GPU_ID
    #translate_opt.beam_size = 1

    return translate_opt, translate_dummy_opt


def init_train_model(c_point, opt, fields):
    model_opt = c_point['opt']
    model_opt.gpuid = opt.gpuid
    return build_model(model_opt, opt, fields, c_point)

def init_translate_model(opt, dummy_opt):
    return onmt.Translator(opt, dummy_opt.__dict__)


# Set the crates of each layer, 
# the pruning will happen in the next forward action
def apply_MP_on_mask(thresholds, mask_dict, orig_dict, layer2group_map_dict, sorted_group_parameters, group_name_list):
    assert len(thresholds) == len(group_name_list)

    threshold_dict = {}
    for i in range(len(group_name_list)):
        group_name = group_name_list[i]
        tmp_threshold_ratio = 1. - thresholds[i]
        _indx = tmp_threshold_ratio * (sorted_group_parameters[group_name].nelement()-1) 
        threshold_dict[group_name] =  sorted_group_parameters[group_name][int(_indx)]

    for the_name, the_param in mask_dict.items():
        group_name = layer2group_map_dict[the_name]
        tmp_v = threshold_dict[group_name]
        tmp_size = the_param.size()
        tmp_m = Variable(orig_dict[the_name].data.new(tmp_size).fill_(tmp_v)) #20191023 lgy
        the_param.data.copy_((orig_dict[the_name].gt(tmp_m) + orig_dict[the_name].lt(tmp_m.neg())).data)

#  evaluate the accuracy of a network with a set of crates respect to a original accuracy
class Statistics(object):
    """
    Train/validate loss statistics.
    """
    def __init__(self, loss=0., n_words=0., n_correct=0.):
        self.loss = loss
        self.n_words = n_words
        self.n_correct = n_correct
        self.n_src_words = 0
        self.start_time = time.time()

    def update(self, stat):
        self.loss += stat.loss
        self.n_words += stat.n_words
        self.n_correct += stat.n_correct

    def accuracy(self):
        return 100 * (self.n_correct / self.n_words)

    def ppl(self):
        return math.exp(min(self.loss / self.n_words, 100))

    def elapsed_time(self):
        return time.time() - self.start_time

    def output(self, epoch, batch, n_batches, start):
        t = self.elapsed_time()
        print(("Epoch %2d, %5d/%5d; acc: %6.2f; ppl: %6.2f; " +
               "%3.0f src tok/s; %3.0f tgt tok/s; %6.0f s elapsed") %
              (epoch, batch,  n_batches,
               self.accuracy(),
               self.ppl(),
               self.n_src_words / (t + 1e-5),
               self.n_words / (t + 1e-5),
               time.time() - start))
        sys.stdout.flush()

    def log(self, prefix, experiment, lr):
        t = self.elapsed_time()
        experiment.add_scalar_value(prefix + "_ppl", self.ppl())
        experiment.add_scalar_value(prefix + "_accuracy", self.accuracy())
        experiment.add_scalar_value(prefix + "_tgtper",  self.n_words / t)
        experiment.add_scalar_value(prefix + "_lr", lr)

def evaluate(thenet, valid_data, fields, batch_size = TEST_BATCH_SIZE, gpu_id=None):
    '''
    translate_opt, translate_dummy_opt = translate_opt_initialize('opennmt_translate_opt.pt', 'opennmt_translate_dummy_opt.pt')
    translator = init_translate_model(translate_opt, translate_dummy_opt)
    del translator.model
    translator.model = thenet
    tt=open(translate_opt.tgt, 'r')
    references = [[t] for t in tt]
    translate_data = onmt.IO.ONMTDataset(
            translate_opt.src, translate_opt.tgt, fields,
            use_filter_pred=False)
    prune_data = onmt.IO.OrderedIterator(
            dataset=translate_data, device=gpu_id,
            batch_size=1, train=False, sort=False,
            shuffle=False)
    tmp_fit = evaluate_trans(translator, references, prune_data, translate_data)
    return tmp_fit# the last two 0.0 reserved for rank number, and sparsity
    '''
    gpu_used = gpu_id if gpu_id is not None else torch.cuda.current_device()
    valid_iter = make_valid_data_iter(valid_data, batch_size, gpu_used)
    valid_loss = make_loss_compute(thenet, fields["tgt"].vocab, valid_data, gpu_used)

    stats = Statistics()

    for batch in valid_iter:
        _, src_lengths = batch.src
        src = onmt.IO.make_features(batch, 'src')
        tgt = onmt.IO.make_features(batch, 'tgt')

        # F-prop through the model.
        outputs, attns, _ = thenet(src, tgt, src_lengths)

        # Compute loss.
        batch_stats = valid_loss.monolithic_compute_loss(
        batch, outputs, attns)
        # Update statistics.
        stats.update(batch_stats)

    return torch.FloatTensor([stats.ppl(), stats.accuracy()])# the last two 0.0 reserved for rank number, and sparsity

def repackage_hidden(h):
    """Wraps hidden states in new Variables, to detach them from their history."""
    if type(h) == Variable:
        return Variable(h.data)
    else:
        return tuple(repackage_hidden(v) for v in h)

def get_batch(source, i, evaluation=False):
    seq_len = min(SEQ_LEN, len(source) - 1 - i)
    data = Variable(source[i:i+seq_len], volatile=evaluation)
    target = Variable(source[i+1:i+1+seq_len].view(-1))
    return data, target

def evaluate_lm(model, data_source, corpus, eval_batch_size):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    total_acc = 0
    ppl = 0
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)
    for i in range(0, data_source.size(0) - 1, SEQ_LEN):
        data, targets = get_batch(data_source, i, evaluation=True)
        output, hidden = model(data, hidden)
        output_flat = output.view(-1, ntokens)
        _, preds = torch.max(output_flat, 1)
        total_acc += (preds == targets).data.cpu().sum()
        total_loss += len(data) * EVAL_FUNC(output_flat, targets).data
        hidden = repackage_hidden(hidden)
    avg_loss = total_loss[0] / len(data_source)
    accuracy = total_acc / data_source.nelement()
    ppl = math.exp(avg_loss)
    #return total_loss[0] / len(data_source)
    return torch.FloatTensor([ppl, accuracy, avg_loss])# the last two 0.0 reserved for rank number, and sparsity


def train(thenet, ntokens, train_data, batch_size, bptt, corpus, grad_clip, log_interval, epoch):
    # Turn on training mode which enables dropout.
    model = thenet.masked_model
    model.train()
    total_loss = 0
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(batch_size)
    for batch, i in enumerate(range(0, train_data.size(0) - 1, bptt)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        model.zero_grad()
        output, hidden = model(data, hidden)
        loss = EVAL_FUNC(output.view(-1, ntokens), targets)
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(model.parameters(), grad_clip)
        # do not upgrade the pruned value, there is implementation in masked_model _apply_mask
        #for param_name, module_tensor in model.named_parameters():
        # if param_name in thenet.map_dict: # ignore no-grouped layers
        #    tmp_mask = thenet.mask_dict[param_name].data
        #    tmp_remain_value = module_tensor.grad.data.masked_select(tmp_mask)
        #    module_tensor.grad.data.zero_()
        #    module_tensor.grad.data.masked_scatter_(tmp_mask, tmp_remain_value)

        for p in model.parameters():
            p.data.add_(-LR, p.grad.data)

        total_loss += loss.data

        if batch % log_interval == 0 and batch > 0:
            cur_loss = total_loss[0] / log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch, len(train_data) // bptt, LR,
                elapsed * 1000 / log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()

# multi process
#------------------------------for parallel--------------------
def init_processes(rank, size, orig_fit, acc_constraint, fn, valid, corpus, es, masked_models, num_runs, final_results, backend='tcp'):
    """ Initialize the distributed environment. """
    os.environ['MASTER_ADDR'] = '127.0.0.1'
    os.environ['MASTER_PORT'] = '29500'
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, size, orig_fit, acc_constraint, valid, corpus, es, masked_models[rank], num_runs, final_results)

def prune_and_eval(rank, size, orig_fit, acc_constraint, valid, corpus, es, ref_model, num_runs, final_results):
    _valid = valid
    gpu_id = GPU_ID
    total_iterations = es.Tmax/es.popsize
    individual_iter_count = 0
    #ref_model = masked_models[rank]
    X = torch.Tensor(copy.deepcopy(es.pop))
    communicate_size = es.n + 4 # the size of tensors transfer accross computers
    communicate_tensor = torch.FloatTensor(communicate_size*[0.])
    fitness_list = []
    itr_best_remain = 0

    if rank == 0: # rank 0 is the main process to collect finesses
        X.share_memory_()
        #fitness_list = [torch.FloatTensor([0.0,0.1,0.2,0.3]).share_memory_() for i in range(size)]
        fitness_list = [torch.FloatTensor(communicate_size*[0.]).share_memory_() for i in range(size)]

    if rank>=1 and rank <size: # split tasks to different GPUs
        gpu_id = other_GPU_IDs[rank-1]

    with cuda.device(gpu_id):

        while (individual_iter_count < total_iterations):
            if rank == 0: # master node
                itr_X = torch.Tensor(es.ask())
                # broadcast the fathers
                X.copy_(itr_X)
                dist.broadcast(itr_X, 0)
            else:
                # recieve fathers from the source process
                dist.broadcast(X, 0)

            # apply MP on model
            x = X.numpy()[rank]
            ref_model.change_mask(x, apply_MP_on_mask)
            
            ref_model.apply_mask()

            # evaluate pruned network
            fitness = evaluate_lm(ref_model.masked_model, _valid, corpus, TEST_BATCH_SIZE)
            communicate_tensor[0] = fitness[0]
            communicate_tensor[1] = fitness[1]
            communicate_tensor[2] = rank
            communicate_tensor[3] = ref_model.get_sparsity()
            for i in range(x.size):
                communicate_tensor[i+4] = X[rank,i]#x[i]

            # sync fitness
            if rank == 0: # collect fitness across processes
                dist.gather(communicate_tensor, gather_list = fitness_list)
            else:
                dist.gather(communicate_tensor, dst = 0)

            # judge new solutions
            if rank == 0: # negatively correlated search in master node
                fit = []
                X_ = []
                for i in range(es.popsize):
                    the_fitness = 100
                    for j in range(len(fitness_list)): # results of fitness evaluation
                        if int(fitness_list[j][2]) == i: # 0:ppl, 1:acc, 2:rank of individual
                            X_.append(fitness_list[j].numpy()[4:])
                            if orig_fit[1] - fitness_list[j][1] <= acc_constraint:
                                the_fitness = -fitness_list[j][3]
                            else:
                                the_fitness = (orig_fit[1] - fitness_list[j][1])/acc_constraint
                            continue
                    fit.append(the_fitness)

                es.tell(X_, fit)

                itr_best_remain = min(fit)

            final_results['result_NCS'].copy_(torch.Tensor(es.result()[0]))
            individual_iter_count += 1

            if rank==0: # record status
                logger.scalar_summary('ncs_%s_fitness' % num_runs, es.result()[1], num_runs*total_iterations + individual_iter_count)
                logger.scalar_summary('ncs_%s_best_itr_remain' % num_runs, itr_best_remain, num_runs*total_iterations + individual_iter_count)
                logger.histo_summary('ncs_%s_pop' % num_runs, es.result()[0], num_runs*total_iterations + individual_iter_count)
                logger.histo_summary('pop of 1', X_[0], num_runs*total_iterations + individual_iter_count)
                logger.scalar_summary('sp of 1', -fitness_list[0][3], num_runs*total_iterations + individual_iter_count)
                logger.scalar_summary('rank of 1', fitness_list[0][2], num_runs*total_iterations + individual_iter_count)
                logger.histo_summary('pop of 2', X_[1], num_runs*total_iterations + individual_iter_count)
                logger.scalar_summary('sp of 2', -fitness_list[1][3], num_runs*total_iterations + individual_iter_count)
                logger.scalar_summary('rank of 2', fitness_list[1][2], num_runs*total_iterations + individual_iter_count)
                #logger.histo_summary('pop of 3', X_[2], num_runs*total_iterations + individual_iter_count)
                #logger.scalar_summary('sp of 3', -fitness_list[2][3], num_runs*total_iterations + individual_iter_count)
                #logger.scalar_summary('rank of 3', fitness_list[2][2], num_runs*total_iterations + individual_iter_count)

    ref_model.clear_cache()

    #print(fitness)

# NCS
def NCS_MP(crates, ncs_stepsize,  masked_models, valid, corpus, acc_constraint, orig_fitvalue, num_runs=0):
    total_time = 0
    total_iteration = 100
    itr_count = 0
    popsize = len(other_GPU_IDs) + 1
    __C = edict()
    __C.parameters = {'reset_xl_to_pop':False,'init_value':crates, 'stepsize':ncs_stepsize, 'bounds':[0.1, 0.99999999], 'ftarget':0, 'tmax':total_iteration*popsize, 'popsize':popsize, 'best_k':1}
    es = ncs.NCS(__C.parameters)

    start_t = time.time()

    print('***************NCS initialization***************')
    ref_net = masked_models[0]
    # 0.0 represents no parameters have been pruned, so it's original fitness
    ref_net.change_mask(len(crates)*[0.0], apply_MP_on_mask)
    ref_net.apply_mask()
    start_fit = evaluate_lm(ref_net.masked_model, valid, corpus, TEST_BATCH_SIZE)
    orignal_fit = orig_fitvalue
    print('start fit: {}'.format(start_fit))
    print('orig fit: {}'.format(orignal_fit))

    ref_net = masked_models[0]
    ref_net.change_mask(crates, apply_MP_on_mask)
    ref_net.apply_mask()
    tmp_fit = evaluate_lm(ref_net.masked_model, valid, corpus, TEST_BATCH_SIZE)
    print("start init threshold:", crates)
    print('Start sparsity: {}%'.format(ref_net.get_sparsity()*100))
    es.set_initFitness(es.popsize*[ref_net.get_sparsity()]) # assume the inital crates store the size of each tensor
    #es.ask()
    #tmp_fit = torch.FloatTensor([0,0,0])

    end_t = time.time()
    total_time = (end_t - start_t)

    print('fit:{}'.format(tmp_fit))
    print('time {}min elapse'.format(total_time/60.))
    print('***************NCS initialization***************')

    ref_net.clear_cache()
    processes = []
    results = {'result_NCS':torch.FloatTensor(crates)}
    results['result_NCS'].share_memory_()

    # paralell individuals
    for rank in range(popsize):
        p = Process(target=init_processes, args=(rank, popsize, orignal_fit, acc_constraint, prune_and_eval, valid, corpus, es, masked_models, num_runs, results))
        p.start()
        processes.append(p)
    for p in processes:
        p.join()

    ref_net.change_mask(results['result_NCS'].numpy(), apply_MP_on_mask)
    ref_net.apply_mask()
    best_prune = evaluate_lm(ref_net.masked_model, valid, corpus, TEST_BATCH_SIZE)
    print('Accuracy:{}=>{}, ppl:{}=>{}, sparsity: {}%'.format(orignal_fit[1], best_prune[1], orignal_fit[0], best_prune[0], ref_net.get_sparsity()*100.))

    logger.scalar_summary('ncs_start_acc', tmp_fit[1], num_runs)
    logger.scalar_summary('ncs_start_ppl', tmp_fit[0], num_runs)
    logger.scalar_summary('ncs_best_acc', best_prune[1], num_runs)
    logger.scalar_summary('ncs_best_ppl', best_prune[0], num_runs)
    if True:
        saved_model_name = 'ncs_pruned_model_%s_iteration%s_%s_%s_acc_cons_%s.pt' % (name_mark, num_runs, Model_type, layer_group_type, str(acc_constraint))
        torch.save(ref_net, cfg.LM_MODEL_TMP_FOLDER+saved_model_name)

    return results['result_NCS'].numpy(), saved_model_name, ref_net

def time_now():
  now = int(round(time.time()*1000))
  return time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(now/1000))

def update_checkpoint(ref_net, run_times, acc_constraint, t=False):
    '''
    t = False 表示 未经过重训练
    '''
    # save model 
    real_model = (ref_net.masked_model.module
                    if isinstance(ref_net.masked_model, nn.DataParallel)
                    else ref_net.masked_model)
    if not t:
        saved_model_name = 'pruned_model_%s_iteration%s_%s_%s_acc_cons_%s.pt' % (name_mark, run_times, Model_type, layer_group_type, str(acc_constraint))
    else:
        saved_model_name = 'retrained_model_%s_iteration%s_%s_%s_acc_cons_%s.pt' % (name_mark, run_times, Model_type, layer_group_type, str(acc_constraint))
    torch.save(ref_net, cfg.LM_MODEL_TMP_FOLDER+saved_model_name)
    return saved_model_name

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    data = data.cuda()
    return data


def main():
    data_path = "{}/data/penn".format(DATA_PATH)
    model_path = "{}/deepModels/SiPO/original_model/language_model/{}".format(MODEL_PATH, 'lm_model_orignal.pt')
    total_times = 12
    run_times = 0
    orginal_acc = 0
    init_threshold = ...
    start_t = time.time()

    # get data
    corpus = data.Corpus(data_path)
    ntokens = len(corpus.dictionary)
    eval_batch_size = TEST_BATCH_SIZE
    train_data = batchify(corpus.train, TRAIN_BATCH_SIZE)
    val_data = batchify(corpus.valid, TEST_BATCH_SIZE)
    valid_data = val_data
    test_data = batchify(corpus.test, TEST_BATCH_SIZE)

    ref_model = None

    # Load the best saved model.

    with cuda.device(GPU_ID):
        ff = open(model_path, 'rb')
        ref_model = torch.load(ff)
        ref_model.eval()
        masked_model = MaskedModel(ref_model, group_dict, cuda.current_device(), cuda.current_device()) # ref_model is at current_device, no copy will happen
        #pdb.set_trace()
        ff.close()
    if GPU_ID:
        cuda.set_device(GPU_ID)

    print(time_now(), "get accuray of no pruning model")
    masked_model.make_evaluable()
    tmp_crate = len(masked_model.group_name_list)*[0]
    masked_model.change_mask(tmp_crate, apply_MP_on_mask)
    masked_model.apply_mask()
    tmp_fit = evaluate_lm(masked_model.masked_model, valid_data, corpus, TEST_BATCH_SIZE)
    # 只需要原始的accuracy
    acc_of_no_prune = tmp_fit[1]
    fit_of_no_prune = tmp_fit
    original_acc = acc_of_no_prune
    pruning_arr = []
    ppl_arr = []
    #acc_of_no_prune = int(acc_of_no_prune*10)/10
    print("init accuracy of model:", acc_of_no_prune)
    print("accuracy constraint:", acc_percent_prune)
    init_threshold = [0]
    print("-----------------------------------------")
    print("-----------------------------------------")
    print("-----------------------------------------")
    SAVE_MODEL_FOLDER = '/fl/checkpoint/language-mode/prune/'
    print("test model---------------")
    LR = LR_INIT
    previous_pr = [0.01] * 4
    previous_fit = [0.01, 12.3]
    best_pr = [0.01] * 4
    best_fit = [0.01, 12.3]
    for prune_rate in range(1, 100):
        tmp_crate = len(masked_model.group_name_list)*[0.01 * prune_rate]
        masked_model.change_mask(tmp_crate, apply_MP_on_mask)
        masked_model.apply_mask()
        tmp_fit = evaluate_lm(masked_model.masked_model, valid_data, corpus, TEST_BATCH_SIZE)
        print("each layer {} \% | {} % in total => validation acc {}\%, validation ppl {}".format(prune_rate, masked_model.get_sparsity()*100, tmp_fit[1]*100., tmp_fit[0]))

        if (tmp_fit[1] + acc_percent_prune) > original_acc:
            best_pr = previous_pr
            best_fit = previous_fit
        
        previous_pr = tmp_crate
        previous_fit = tmp_fit
        

        
        test_fit = evaluate_lm(masked_model.masked_model, test_data, corpus, TEST_BATCH_SIZE)
        print("{} \% => validation acc {}\%, validation ppl {}".format(best_pr[0], best_fit[1]*100., best_fit[0]))
        print("{} \% => test acc {}\%, test ppl {}".format(best_pr[0], test_fit[1]*100., test_fit[0]))
        print('==============================')
        masked_model.apply_mask_init()
        init_threshold = best_pr
        saved_model_name = 'ncs_pruned_model_%s_iteration%s_%s_%s_acc_cons_%s.pt' % (name_mark, run_times, Model_type, layer_group_type, str(acc_percent_prune))
        torch.save(masked_model.masked_model, SAVE_MODEL_FOLDER+saved_model_name)

        #--------------- start retraining --------------
        model_for_train = masked_model
        
        with open(SAVE_MODEL_FOLDER + saved_model_name, 'rb') as f:
            model_tmp_load = torch.load(f)
            model_for_train.masked_model = model_tmp_load

        model_for_train.change_mask(init_threshold, apply_MP_on_mask)
        model_for_train.apply_mask()
        model_for_train.make_trainable()
        recovered = False
        best_val_loss = None
        train(model_for_train, ntokens, train_data, TRAIN_BATCH_SIZE, SEQ_LEN, corpus, GRAD_CLIP, TRAIN_LOG_INTERVAL, prune_rate)
                

        print(time_now(), "finish retraining ")
        print("------------Accuracy recorverd!--------------------")
        print("recovered accuracy (>= {})".format(acc_of_no_prune))
        model_for_train.make_evaluable()
        model_for_train.apply_mask()

        ref_model = model_for_train.masked_model

        print("validate acc of the model---------------")
        tmp_fit = evaluate_lm(ref_model, valid_data, corpus, TEST_BATCH_SIZE)
        print('ref_model','acc (%.4f), ppl (%.4f)' % (tmp_fit[1], tmp_fit[0]))

        print("-------------print TEST  evaluation info ---------------")
        tmp_fit = evaluate_lm(ref_model, test_data, corpus, TEST_BATCH_SIZE)
        print('percentage %s => acc (%.4f), ppl (%.4f)' % (init_threshold[0]*100, tmp_fit[1], tmp_fit[0]))
        masked_model = model_for_train


        print('==============================')
        print("The best pruning rates are: {}".format(best_pr))
        
        masked_model.change_mask(best_pr, apply_MP_on_mask)
        masked_model.apply_mask()
        test_fit = evaluate_lm(masked_model.masked_model, test_data, corpus, TEST_BATCH_SIZE)
        print("{} \% => validation acc {}\%, validation ppl {}".format(best_pr[0], best_fit[1]*100., best_fit[0]))
        print("{} \% => test acc {}\%, test ppl {}".format(best_pr[0], test_fit[1]*100., test_fit[0]))
        print('==============================')


if __name__ == "__main__":
    main()