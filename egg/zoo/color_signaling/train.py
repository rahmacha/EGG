# Copyright (c) Facebook, Inc. and its affiliates.

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.utils.data
from torch import nn
from torch.nn import functional as F
import torch.distributions
import egg.core as core
import argparse
import numpy as np
from sklearn.metrics import mutual_info_score

import egg.core as core
from egg.core import EarlyStopperLoss,EarlyStopperAccuracy
from egg.zoo.color_signaling.data import ColorData, ColorIterator, build_distance_matrix
from egg.zoo.color_signaling.archs import Sender, Receiver

N_COLOR_IDS = 330

def get_params(params):
    parser = argparse.ArgumentParser()
    parser.add_argument('--temperature', type=float, default=1.0,
                        help="GS temperature for the sender (default: 1.0)")
    parser.add_argument('--mode', type=str, choices=['gs', 'rf'], default='rf')
    parser.add_argument('--sender_entropy_coeff', type=float, default=5e-2)
    parser.add_argument('--early_stopping_delta', type=float, default=1e-10,
                        help="Early stopping delta on loss (default: 1e-5)")
    parser.add_argument('--early_stopping_patience', type=int, default=100,
                        help="Early stopping patience on loss (default: 10)")
    parser.add_argument('--batches_per_epoch', type=int, default=100,
                        help='Number of batches per epoch (default: 100)')
    parser.add_argument('--n_distractor', type=int, default=1)
    parser.add_argument('--percentile', type=float, default=50.0)
    parser.add_argument('--receiver_hidden', type=int, default=100,
                        help='Size of the hidden layer of Receiver (default: 100)')
    parser.add_argument('--early_stopping_thr', type=float, default=0.9999,
                        help="Early stopping threshold on accuracy (default: 0.9999)")


    args = core.init(arg_parser=parser, params=params)
    return args

def compute_Entropy(variables):
    dico = {}
    for variable in variables:
        m = variable.argmax().detach().item()
        if m in dico.keys():
            dico[m] += 1.
        else:
            dico[m] = 1.
    #freq_dico = {}
    entropy = 0.
    for key, value in dico.items():
        tmp = value/sum([*dico.values()])
        #freq_dico[key] = tmp
        entropy += -tmp*np.log(tmp)
    return entropy

def compute_MI(variables1, variables2):
    return mutual_info_score(variables1, variables2)

def findposition(value, alist):
    if value>=alist[-1]:
        return len(alist)

    for i, element in enumerate(alist):
        if value >= element and value < alist[i+1]:
            return i

def cross_entropy(_sender_input,  _message, _receiver_input, receiver_output, _labels):
    acc = (receiver_output.argmax(dim=1) == _labels).detach().float()
    loss = F.cross_entropy(receiver_output, _labels, reduction="none")

    H_m = compute_Entropy(_message)
    messages = []
    for m in _message:
        messages.append(m.argmax().detach().item())

    color_ids = _sender_input[:,0].long()
    I_m_inp = compute_MI(messages, color_ids.detach().tolist())
    I_m_out = compute_MI(messages, receiver_output.detach().argmax(dim=1).tolist())

    return loss, {'acc': acc, 'Hm': H_m, 'I_m_inp': I_m_inp, 'I_m_out': I_m_out}


def dump(game, test_data, device):
    game.eval()
    dataset = [x for x in test_data][0]
    sender_inp = dataset[0].to(device)
    labels = dataset[1].to(device)
    receiver_inp = dataset[2].to(device)
    with torch.no_grad():
        messages = game.sender(sender_inp)
        receiver_outputs = game.receiver(messages, receiver_inp)
    unif_acc = 0.0
    for (input_sender, message, output, label, input_receiver) in zip(sender_inp, messages, receiver_outputs, labels, receiver_inp):
        out = output.argmax(dim=0)
        acc = label==out
        unif_acc+=acc.item()
        s_inp = input_sender[0].long() # # ID
        r_inp = input_receiver[:,0].long() # # ID
        print(f'input sender: {s_inp.item()} | input receiver: {[r.item() for r in r_inp]} -> message: {message.argmax().item()} -> output: {out.item()}', flush=True)
    print(f'acc={unif_acc/sender_inp.size(0)}')


def main(params):
    opts = get_params(params)
    device = opts.device

    data = ColorData()
    distance_matrix = build_distance_matrix(data)
    precentile = np.percentile(distance_matrix, opts.percentile)

    train_loader = ColorIterator(n_distractor=opts.n_distractor, n_batches_per_epoch=opts.batches_per_epoch, seed=opts.random_seed, \
                                    batch_size=opts.batch_size, distance_matrix=distance_matrix, min_value=precentile, data=data)
    # Same validation across runs by fixing the seed
    val_loader = ColorIterator(n_distractor=opts.n_distractor, n_batches_per_epoch=1, train=False, seed=1, \
                                    batch_size=len(data), distance_matrix=distance_matrix, min_value=precentile, data=data)

    # initialize the agents and the game
    sender = Sender(opts.vocab_size)  # the "data" transform part of an agent
    receiver = Receiver(opts.receiver_hidden)
    receiver = core.SymbolReceiverWrapper(receiver, vocab_size=opts.vocab_size, agent_input_size=opts.receiver_hidden)


    if opts.mode == 'gs':
        sender = core.GumbelSoftmaxWrapper(sender, temperature=opts.temperature)
        game = core.SymbolGameGS(sender, receiver, cross_entropy)
    else:
        sender = core.ReinforceWrapper(sender)
        receiver = core.ReinforceDeterministicWrapper(receiver)
        game = core.SymbolGameReinforce(sender, receiver, cross_entropy, opts.sender_entropy_coeff)


    optimizer = core.build_optimizer(game.parameters())

    callbacks = [
        EarlyStopperAccuracy(opts.early_stopping_thr),
        EarlyStopperLoss(delta=opts.early_stopping_delta, patience=opts.early_stopping_patience),
        core.ConsoleLogger(print_train_loss=False, as_json=True),
    ]


    # initialize and launch the trainer
    trainer = core.Trainer(game=game, optimizer=optimizer, train_data=train_loader, validation_data=val_loader, callbacks=callbacks)
    trainer.train(n_epochs=opts.n_epochs)

    dump(game, val_loader, device)
    core.close()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
