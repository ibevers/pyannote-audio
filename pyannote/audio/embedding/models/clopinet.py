#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2017-2018 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr


import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import PackedSequence
from torch.nn.utils.rnn import pad_packed_sequence


class ClopiNet(nn.Module):
    """ClopiNet sequence embedding

    RNN          ⎤
      » RNN      ⎥ » MLP » Weight » temporal pooling › normalize
           » RNN ⎦

    Parameters
    ----------
    n_features : int
        Input feature dimension.
    rnn : {'LSTM', 'GRU'}, optional
        Defaults to 'LSTM'.
    recurrent : list, optional
        List of hidden dimensions of stacked recurrent layers. Defaults to
        [64, 64, 64], i.e. three recurrent layers with hidden dimension of 64.
    bidirectional : bool, optional
        Use bidirectional recurrent layers. Defaults to False, i.e. use
        mono-directional RNNs.
    pooling : {'sum', 'max'}
        Temporal pooling strategy. Defaults to 'sum'.
    instance_normalize : boolean, optional
        Apply mean/variance normalization on input sequences.
    batch_normalize : boolean, optional
        Set to False to not apply batch normalization before embedding
        normalization. Defaults to True.
    normalize : {False, 'sphere', 'ball', 'ring'}, optional
        Normalize embeddings.
    weighted : bool, optional
        Add dimension-wise trainable weights. Defaults to False.
    linear : list, optional
        List of hidden dimensions of linear layers. Defaults to none.
    attention : list of int, optional
        List of hidden dimensions of attention linear layers (e.g. [16, ]).
        Defaults to no attention.

    Usage
    -----
    >>> model = ClopiNet(n_features)
    >>> embedding = model(sequence)
    """

    def __init__(self, n_features,
                 rnn='LSTM', recurrent=[64, 64, 64], bidirectional=False,
                 pooling='sum', instance_normalize=False, batch_normalize=True,
                 normalize=False, weighted=False, linear=None, attention=None):

        super(ClopiNet, self).__init__()

        self.n_features = n_features
        self.rnn = rnn
        self.recurrent = recurrent
        self.bidirectional = bidirectional
        self.pooling = pooling
        self.instance_normalize = instance_normalize
        self.batch_normalize = batch_normalize
        self.normalize = normalize
        self.weighted = weighted
        self.linear = [] if linear is None else linear
        self.attention = [] if attention is None else attention

        self.num_directions_ = 2 if self.bidirectional else 1

        if self.pooling not in {'sum', 'max'}:
            raise ValueError('"pooling" must be one of {"sum", "max"}')

        # create list of recurrent layers
        self.recurrent_layers_ = []
        input_dim = self.n_features
        for i, hidden_dim in enumerate(self.recurrent):
            if self.rnn == 'LSTM':
                recurrent_layer = nn.LSTM(input_dim, hidden_dim,
                                          bidirectional=self.bidirectional,
                                          batch_first=True)
            elif self.rnn == 'GRU':
                recurrent_layer = nn.GRU(input_dim, hidden_dim,
                                         bidirectional=self.bidirectional,
                                         batch_first=True)
            else:
                raise ValueError('"rnn" must be one of {"LSTM", "GRU"}.')
            # TODO. use nn.ModuleList instead
            self.add_module('recurrent_{0}'.format(i), recurrent_layer)
            self.recurrent_layers_.append(recurrent_layer)
            input_dim = hidden_dim * (2 if self.bidirectional else 1)

        # the output of recurrent layers are concatenated so the input
        # dimension of subsequent linear layers is the sum of their output
        # dimension
        input_dim = sum(self.recurrent) * (2 if self.bidirectional else 1)

        if self.weighted:
            self.alphas_ = nn.Parameter(torch.ones(input_dim))

        # create list of linear layers
        self.linear_layers_ = []
        for i, hidden_dim in enumerate(self.linear):
            linear_layer = nn.Linear(input_dim, hidden_dim, bias=True)
            self.add_module('linear_{0}'.format(i), linear_layer)
            self.linear_layers_.append(linear_layer)
            input_dim = hidden_dim

        # batch normalization ~= embeddings whitening.
        if self.batch_normalize:
            self.batch_norm_ = nn.BatchNorm1d(input_dim, eps=1e-5,
                                              momentum=0.1, affine=False)

        if self.normalize in {'ball', 'ring'}:
            self.norm_batch_norm_ = nn.BatchNorm1d(1, eps=1e-5, momentum=0.1,
                                                   affine=False)

        # create attention layers
        self.attention_layers_ = []
        if not self.attention:
            return

        input_dim = self.n_features
        for i, hidden_dim in enumerate(self.attention):
            attention_layer = nn.Linear(input_dim, hidden_dim, bias=True)
            self.add_module('attention_{0}'.format(i), attention_layer)
            self.attention_layers_.append(attention_layer)
            input_dim = hidden_dim
        if input_dim > 1:
            attention_layer = nn.Linear(input_dim, 1, bias=True)
            self.add_module('attention_{0}'.format(len(self.attention)),
                            attention_layer)
            self.attention_layers_.append(attention_layer)

    @property
    def output_dim(self):
        if self.linear:
            return self.linear[-1]
        return sum(self.recurrent) * (2 if self.bidirectional else 1)

    def forward(self, sequence):

        packed_sequences = isinstance(sequence, PackedSequence)

        if packed_sequences:
            _, n_features = sequence.data.size()
            batch_size = sequence.batch_sizes[0].item()
            device = sequence.data.device
        else:
            # check input feature dimension
            batch_size, _, n_features = sequence.size()
            device = sequence.device

        if n_features != self.n_features:
            msg = 'Wrong feature dimension. Found {0}, should be {1}'
            raise ValueError(msg.format(n_features, self.n_features))

        output = sequence

        if self.instance_normalize:
            sequence = sequence.transpose(1, 2)
            sequence = F.instance_norm(sequence)
            sequence = sequence.transpose(1, 2)

        if self.weighted:
            self.alphas_ = self.alphas_.to(device)

        outputs = []
        # stack recurrent layers
        for hidden_dim, layer in zip(self.recurrent, self.recurrent_layers_):

            if self.rnn == 'LSTM':
                # initial hidden and cell states
                h = torch.zeros(self.num_directions_, batch_size, hidden_dim,
                                device=device, requires_grad=False)
                c = torch.zeros(self.num_directions_, batch_size, hidden_dim,
                                device=device, requires_grad=False)
                hidden = (h, c)

            elif self.rnn == 'GRU':
                # initial hidden state
                hidden = torch.zeros(
                    self.num_directions_, batch_size, hidden_dim,
                    device=device, requires_grad=False)

            # apply current recurrent layer and get output sequence
            output, _ = layer(output, hidden)

            outputs.append(output)

        if packed_sequences:
            outputs, lengths = zip(*[pad_packed_sequence(o, batch_first=True)
                                     for o in outputs])

        # concatenate outputs
        output = torch.cat(outputs, dim=2)
        # batch_size, n_samples, dimension

        if self.weighted:
            output = output * self.alphas_

        # stack linear layers
        for hidden_dim, layer in zip(self.linear, self.linear_layers_):

            # apply current linear layer
            output = layer(output)

            # apply non-linear activation function
            output = F.tanh(output)

        # n_samples, batch_size, dimension

        if self.attention_layers_:
            attn = sequence
            for layer, hidden_dim in zip(self.attention_layers_,
                                         self.attention + [1]):
                attn = layer(attn)
                attn = F.tanh(attn)

            if packed_sequences:
                msg = ('attention is not yet implemented '
                       'for variable length sequences.')
                raise NotImplementedError(msg)
            attn = F.softmax(attn, dim=1)
            output = output * attn

        # average temporal pooling
        if self.pooling == 'sum':
            output = output.sum(dim=1)
        elif self.pooling == 'max':
            if packed_sequences:
                msg = ('"max" pooling is not yet implemented '
                       'for variable length sequences.')
                raise NotImplementedError(msg)
            output, _ = output.max(dim=1)

        # batch_size, dimension

        # batch normalization
        if self.batch_normalize:
            output = self.batch_norm_(output)

        if self.normalize:
            norm = torch.norm(output, 2, 1, keepdim=True)

        if self.normalize == 'sphere':
            output = output / norm

        elif self.normalize == 'ball':
            output = output / norm * F.sigmoid(self.norm_batch_norm_(norm))

        elif self.normalize == 'ring':
            norm_ = self.norm_batch_norm_(norm)
            output = output / norm * (1 + F.sigmoid(self.norm_batch_norm_(norm)))

        # batch_size, dimension

        return output