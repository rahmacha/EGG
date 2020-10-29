# Copyright (c) Facebook, Inc. and its affiliates.

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import uuid
import pathlib
from typing import List, Optional
import pickle

import torch
from torch.utils.data import DataLoader

from .util import get_opts, move_to
from .callbacks import Callback, ConsoleLogger, Checkpoint, Checkpoint_agents, CheckpointSaver
from collections import OrderedDict


def _add_dicts(a, b):
    result = dict(a)
    for k, v in b.items():
        result[k] = result.get(k, 0) + v
    return result


def _div_dict(d, n):
    result = dict(d)
    for k in result:
        result[k] /= n
    return result


def get_gradients(game, optimizer, batch):
    optimizer.zero_grad()
    optimized_loss, _ = game(*batch)
    optimized_loss.backward()
    tmp_dict = {}
    for name, param in game.sender.named_parameters():
        if param.requires_grad:
            tmp_dict[name] = param.grad.cpu().numpy()
    return tmp_dict

class Trainer:
    """
    Implements the training logic. Some common configuration (checkpointing frequency, path, validation frequency)
    is done by checking util.common_opts that is set via the CL.
    """
    def __init__(
            self,
            game: torch.nn.Module,
            optimizer: torch.optim.Optimizer,
            train_data: DataLoader,
            validation_data: Optional[DataLoader] = None,
            device: torch.device = None,
            callbacks: Optional[List[Callback]] = None
    ):
        """
        :param game: A nn.Module that implements forward(); it is expected that forward returns a tuple of (loss, d),
            where loss is differentiable loss to be minimized and d is a dictionary (potentially empty) with auxiliary
            metrics that would be aggregated and reported
        :param optimizer: An instance of torch.optim.Optimizer
        :param train_data: A DataLoader for the training set
        :param validation_data: A DataLoader for the validation set (can be None)
        :param device: A torch.device on which to tensors should be stored
        :param callbacks: A list of egg.core.Callback objects that can encapsulate monitoring or checkpointing
        """
        self.game = game
        self.optimizer = optimizer
        self.train_data = train_data
        self.validation_data = validation_data
        common_opts = get_opts()
        self.validation_freq = common_opts.validation_freq
        self.device = common_opts.device if device is None else device
        self.game.to(self.device)
        # NB: some optimizers pre-allocate buffers before actually doing any steps
        # since model is placed on GPU within Trainer, this leads to having optimizer's state and model parameters
        # on different devices. Here, we protect from that by moving optimizer's internal state to the proper device
        self.optimizer.state = move_to(self.optimizer.state, self.device)
        self.should_stop = False
        self.start_epoch = 0  # Can be overwritten by checkpoint loader
        self.callbacks = callbacks

        if common_opts.load_from_checkpoint is not None:
            print(f"# Initializing model, trainer, and optimizer from {common_opts.load_from_checkpoint}")
            self.load_from_checkpoint(common_opts.load_from_checkpoint)

        if common_opts.preemptable:
            assert common_opts.checkpoint_dir, 'checkpointing directory has to be specified'
            d = self._get_preemptive_checkpoint_dir(common_opts.checkpoint_dir)
            self.checkpoint_path = d
            self.load_from_latest(d)
            checkpointer = CheckpointSaver(self.checkpoint_path, common_opts.checkpoint_freq)
            self.callbacks.append(checkpointer)
        else:
            self.checkpoint_path = None if common_opts.checkpoint_dir is None \
                else pathlib.Path(common_opts.checkpoint_dir)

        if self.checkpoint_path:
            checkpointer = CheckpointSaver(checkpoint_path=self.checkpoint_path, checkpoint_freq=common_opts.checkpoint_freq)
            self.callbacks.append(checkpointer)

        if self.callbacks is None:
            self.callbacks = [
                ConsoleLogger(print_train_loss=False, as_json=False),
            ]

    def _get_preemptive_checkpoint_dir(self, checkpoint_root):
        if 'SLURM_JOB_ID' not in os.environ:
            print('Preemption flag set, but I am not running under SLURM?')

        job_id = os.environ.get('SLURM_JOB_ID', uuid.uuid4())
        task_id = os.environ.get('SLURM_PROCID', 0)

        d = pathlib.Path(checkpoint_root) / f'{job_id}_{task_id}'
        d.mkdir(exist_ok=True)

        return d

    def eval(self):
        mean_loss = 0.0
        mean_rest = {}

        n_batches = 0
        self.game.eval()
        with torch.no_grad():
            for batch in self.validation_data:
                batch = move_to(batch, self.device)
                optimized_loss, rest = self.game(*batch)
                mean_loss += optimized_loss
                mean_rest = _add_dicts(mean_rest, rest)
                n_batches += 1

        mean_loss /= n_batches
        mean_rest = _div_dict(mean_rest, n_batches)

        return mean_loss.item(), mean_rest

    def train_epoch(self):
        mean_loss = 0
        mean_rest = {}
        n_batches = 0
        self.game.train()
        for batch in self.train_data:
            #print('train')
            #print(batch)
            self.optimizer.zero_grad()
            batch = move_to(batch, self.device)
            optimized_loss, rest = self.game(*batch)
            mean_rest = _add_dicts(mean_rest, rest)
            optimized_loss.backward()
            self.optimizer.step()
            """
            for name, param in self.game.named_parameters():
                if param.requires_grad:
                    print(name)
                    if len(param.grad)>0:
                        print(param.grad[0])
            """

            n_batches += 1
            mean_loss += optimized_loss

        mean_loss /= n_batches
        mean_rest = _div_dict(mean_rest, n_batches)
        return mean_loss.item(), mean_rest

    def train(self, n_epochs):
        for callback in self.callbacks:
            callback.on_train_begin(self)

        for epoch in range(self.start_epoch, n_epochs):
            for callback in self.callbacks:
                callback.on_epoch_begin()

            train_loss, train_rest = self.train_epoch()

            for callback in self.callbacks:
                callback.on_epoch_end(train_loss, train_rest)

            if self.validation_data is not None and self.validation_freq > 0 and epoch % self.validation_freq == 0:
                for callback in self.callbacks:
                    callback.on_test_begin()
                validation_loss, rest = self.eval()
                for callback in self.callbacks:
                    callback.on_test_end(validation_loss, rest)

            if self.should_stop:
                break

        for callback in self.callbacks:
            callback.on_train_end()

    def load(self, checkpoint: Checkpoint):
        self.game.load_state_dict(checkpoint.model_state_dict)
        self.optimizer.load_state_dict(checkpoint.optimizer_state_dict)
        self.starting_epoch = checkpoint.epoch

    def load_from_checkpoint(self, path):
        """
        Loads the game, agents, and optimizer state from a file
        :param path: Path to the file
        """
        print(f'# loading trainer state from {path}')
        checkpoint = torch.load(path)
        self.load(checkpoint)

    def load_from_latest(self, path):
        latest_file, latest_time = None, None

        for file in path.glob('*.tar'):
            creation_time = os.stat(file).st_ctime
            if latest_time is None or creation_time > latest_time:
                latest_file, latest_time = file, creation_time

        if latest_file is not None:
            self.load_from_checkpoint(latest_file)



class SaverTrainer(Trainer):
    """
    Implements the training logic. Some common configuration (checkpointing frequency, path, validation frequency)
    is done by checking util.common_opts that is set via the CL.
    This trainer saves the gradient before each backprop.
    """
    def __init__(
            self,
            game: torch.nn.Module,
            optimizer: torch.optim.Optimizer,
            train_data: DataLoader,
            validation_data: Optional[DataLoader] = None,
            device: torch.device = None,
            callbacks: Optional[List[Callback]] = None,
            N: int = 10,
    ):
        """
        :param game: A nn.Module that implements forward(); it is expected that forward returns a tuple of (loss, d),
            where loss is differentiable loss to be minimized and d is a dictionary (potentially empty) with auxiliary
            metrics that would be aggregated and reported
        :param optimizer: An instance of torch.optim.Optimizer
        :param train_data: A DataLoader for the training set
        :param validation_data: A DataLoader for the validation set (can be None)
        :param device: A torch.device on which to tensors should be stored
        :param callbacks: A list of egg.core.Callback objects that can encapsulate monitoring or checkpointing
        """
        super().__init__(game, optimizer, train_data, validation_data, device, callbacks)
        self.N = N
        self.checkpoint_path.mkdir(exist_ok=True)

        # Save initialization model
        checkpoint =  Checkpoint_agents(sender_state_dict=self.game.sender.state_dict(),
                        receiver_state_dict=self.game.receiver.state_dict(),
                        optimizer_state_dict=self.optimizer.state_dict())
        
        path = self.checkpoint_path / f'initial.tar'
        torch.save(checkpoint, path)

    def train_epoch(self, epoch):
        mean_loss = 0
        mean_rest = {}
        n_batches = 0
        self.game.train()
        batches_gradient_Nforward = {}
        batches_gradient_1forward = {}
        for batch in self.train_data:
            batch = move_to(batch, self.device)

            # Get sender gradients after multiple forwards (to compute bias)
            tmp_list = []
            for _ in range(self.N):
                self.optimizer.zero_grad()
                optimized_loss, _ = self.game(*batch)
                optimized_loss.backward()
                tmp_dict = {}
                for name, param in self.game.sender.named_parameters():
                    if param.requires_grad:
                        tmp_dict[name] = param.grad.cpu().numpy()
                tmp_list.append(tmp_dict)
            batches_gradient_Nforward[n_batches] = tmp_list

            # Get the gradient to backprob (to compute variance)
            self.optimizer.zero_grad()
            optimized_loss, rest = self.game(*batch)
            mean_rest = _add_dicts(mean_rest, rest)
            optimized_loss.backward()
            forward1_dict = {}
            for name, param in self.game.sender.named_parameters():
                if param.requires_grad:
                    forward1_dict[name] = param.grad.cpu().numpy()
            batches_gradient_1forward[n_batches] = forward1_dict
            # Backprob
            self.optimizer.step()
            # Save updated model
            checkpoint =  Checkpoint_agents(sender_state_dict=self.game.sender.state_dict(),
                          receiver_state_dict=self.game.receiver.state_dict(),
                          optimizer_state_dict=self.optimizer.state_dict())
            
            path = self.checkpoint_path / f'epoch{epoch}_batch{n_batches}.tar'
            torch.save(checkpoint, path)

            mean_loss += optimized_loss
            n_batches += 1

        mean_loss /= n_batches
        mean_rest = _div_dict(mean_rest, n_batches)
        return mean_loss.item(), mean_rest, batches_gradient_Nforward, batches_gradient_1forward

    def train(self, n_epochs):
        for callback in self.callbacks:
            callback.on_train_begin(self)

        epoch_gradient  = {}
        for epoch in range(self.start_epoch, n_epochs):
            for callback in self.callbacks:
                callback.on_epoch_begin()

            train_loss, train_rest, gradients_Nsamples, gradients_1sample = self.train_epoch(epoch)
            epoch_gradient[epoch] = {'Nsamples': gradients_Nsamples, '1sample': gradients_1sample}

            for callback in self.callbacks:
                callback.on_epoch_end(train_loss, train_rest)

            if self.validation_data is not None and self.validation_freq > 0 and epoch % self.validation_freq == 0:
                for callback in self.callbacks:
                    callback.on_test_begin()
                validation_loss, rest = self.eval()
                for callback in self.callbacks:
                    callback.on_test_end(validation_loss, rest)

            if self.should_stop:
                break
    
        # Save all gradients
        path = self.checkpoint_path / f'saver_gradient.pkl'
        pickle.dump(epoch_gradient, open(path, 'wb'))   

        for callback in self.callbacks:
            callback.on_train_end()


class LoaderTrainer(Trainer):
    """
    Implements the training logic. Some common configuration (checkpointing frequency, path, validation frequency)
    is done by checking util.common_opts that is set via the CL.
    This trainer saves the gradient before each backprop.
    """
    def __init__(
            self,
            game: torch.nn.Module,
            optimizer: torch.optim.Optimizer,
            train_data: DataLoader,
            validation_data: Optional[DataLoader] = None,
            device: torch.device = None,
            callbacks: Optional[List[Callback]] = None,
            loader_path: str = None,
            N: int = 10,
    ):
        """
        :param game: A nn.Module that implements forward(); it is expected that forward returns a tuple of (loss, d),
            where loss is differentiable loss to be minimized and d is a dictionary (potentially empty) with auxiliary
            metrics that would be aggregated and reported
        :param optimizer: An instance of torch.optim.Optimizer
        :param train_data: A DataLoader for the training set
        :param validation_data: A DataLoader for the validation set (can be None)
        :param device: A torch.device on which to tensors should be stored
        :param callbacks: A list of egg.core.Callback objects that can encapsulate monitoring or checkpointing
        """
        """
        :param game: A nn.Module that implements forward(); it is expected that forward returns a tuple of (loss, d),
            where loss is differentiable loss to be minimized and d is a dictionary (potentially empty) with auxiliary
            metrics that would be aggregated and reported
        :param optimizer: An instance of torch.optim.Optimizer
        :param train_data: A DataLoader for the training set
        :param validation_data: A DataLoader for the validation set (can be None)
        :param device: A torch.device on which to tensors should be stored
        :param callbacks: A list of egg.core.Callback objects that can encapsulate monitoring or checkpointing
        """
        self.game = game
        self.optimizer = optimizer
        self.train_data = train_data
        self.validation_data = validation_data
        common_opts = get_opts()
        self.validation_freq = common_opts.validation_freq
        self.device = common_opts.device if device is None else device
        self.game.to(self.device)
        # NB: some optimizers pre-allocate buffers before actually doing any steps
        # since model is placed on GPU within Trainer, this leads to having optimizer's state and model parameters
        # on different devices. Here, we protect from that by moving optimizer's internal state to the proper device
        self.optimizer.state = move_to(self.optimizer.state, self.device)
        self.should_stop = False
        self.start_epoch = 0  # Can be overwritten by checkpoint loader
        self.callbacks = callbacks

        d = self._get_preemptive_checkpoint_dir(common_opts.checkpoint_dir)
        self.checkpoint_path = d

        if self.checkpoint_path:
            checkpointer = CheckpointSaver(checkpoint_path=self.checkpoint_path, checkpoint_freq=common_opts.checkpoint_freq)
            self.callbacks.append(checkpointer)
        self.checkpoint_path.mkdir(exist_ok=True)

        if self.callbacks is None:
            self.callbacks = [
                ConsoleLogger(print_train_loss=False, as_json=False),
            ]

        self.N = N
        self.loader_path = loader_path

        # Make sure we start from the same initialization
        if self.loader_path:
            path = self.loader_path / f'initial.tar'
            self.load_from_checkpoint(path)
        else:
            print('need to specify loader path')
            raise Exception


    def train_epoch(self, epoch):
        mean_loss = 0
        mean_rest = {}
        n_batches = 0
        self.game.train()
        batches_gradient_Nforward = {}
        for batch in self.train_data:
            batch = move_to(batch, self.device)

            # Load model
            if self.loader_path:
                path = self.loader_path / f'epoch{epoch}_batch{n_batches}.tar'
                self.load_from_checkpoint(path)
            else:
                print('need to specify loader path')
                raise Exception

            # Get sender gradients after multiple forwards (to compute bias)
            tmp_list = []
            for _ in range(self.N):
                self.optimizer.zero_grad()
                optimized_loss, _ = self.game(*batch)
                optimized_loss.backward()
                tmp_dict = {}
                for name, param in self.game.sender.named_parameters():
                    if param.requires_grad:
                        tmp_dict[name] = param.grad.cpu().numpy()
                tmp_list.append(tmp_dict)
            batches_gradient_Nforward[n_batches] = tmp_list

            mean_loss += optimized_loss
            n_batches += 1

        mean_loss /= n_batches
        mean_rest = _div_dict(mean_rest, n_batches)
        return mean_loss.item(), mean_rest, batches_gradient_Nforward

    def train(self, n_epochs):
        for callback in self.callbacks:
            callback.on_train_begin(self)

        epoch_gradient  = {}
        for epoch in range(self.start_epoch, n_epochs):
            for callback in self.callbacks:
                callback.on_epoch_begin()

            train_loss, train_rest, gradients_Nsamples = self.train_epoch(epoch)
            epoch_gradient[epoch] = gradients_Nsamples

            for callback in self.callbacks:
                callback.on_epoch_end(train_loss, train_rest)

            if self.validation_data is not None and self.validation_freq > 0 and epoch % self.validation_freq == 0:
                for callback in self.callbacks:
                    callback.on_test_begin()
                validation_loss, rest = self.eval()
                for callback in self.callbacks:
                    callback.on_test_end(validation_loss, rest)

            if self.should_stop:
                break
    
        # Save all gradients
        path = self.checkpoint_path / f'loader_gradient.pkl'
        pickle.dump(epoch_gradient, open(path, 'wb'))   

        for callback in self.callbacks:
            callback.on_train_end()

    def load(self, checkpoint: Checkpoint):
        self.game.sender.load_state_dict(checkpoint.sender_state_dict)
        # TODO: hard coded here
        compatible_state_dict = OrderedDict()
        for key, values in checkpoint.receiver_state_dict.items():
            compatible_state_dict[f'agent.{key}'] = values
        self.game.receiver.load_state_dict(compatible_state_dict)
        self.optimizer.load_state_dict(checkpoint.optimizer_state_dict)
        
        

class SaverLoaderTrainer(Trainer):
    """
    Implements the training logic. Some common configuration (checkpointing frequency, path, validation frequency)
    is done by checking util.common_opts that is set via the CL.
    This trainer saves the gradient before each backprop.
    """
    def __init__(
            self,
            game: torch.nn.Module,
            optimizer: torch.optim.Optimizer,
            train_data: DataLoader,
            rf_game:  torch.nn.Module,
            rf_optimizer: torch.optim.Optimizer,
            validation_data: Optional[DataLoader] = None,
            device: torch.device = None,
            callbacks: Optional[List[Callback]] = None,
            N: int = 10,
    ):
        """
        :param game: A nn.Module that implements forward(); it is expected that forward returns a tuple of (loss, d),
            where loss is differentiable loss to be minimized and d is a dictionary (potentially empty) with auxiliary
            metrics that would be aggregated and reported
        :param optimizer: An instance of torch.optim.Optimizer
        :param train_data: A DataLoader for the training set
        :param validation_data: A DataLoader for the validation set (can be None)
        :param device: A torch.device on which to tensors should be stored
        :param callbacks: A list of egg.core.Callback objects that can encapsulate monitoring or checkpointing
        """
        super().__init__(game, optimizer, train_data, validation_data, device, callbacks)
        self.N = N
        self.checkpoint_path.mkdir(exist_ok=True)
        self.rf_game = rf_game
        self.rf_optimizer = rf_optimizer
        self.rf_game.to(self.device)
        self.rf_optimizer.state = move_to(self.rf_optimizer.state, self.device)

    def train_epoch(self, epoch):
        mean_loss = 0
        mean_rest = {}
        n_batches = 0
        self.game.train()
        RF_batches_gradient_Nforward = {}
        batches_gradient_Nforward = {}
        batches_gradient_1forward = {}
        for batch in self.train_data:
            batch = move_to(batch, self.device)

            # Get sender gradients after multiple forwards (to compute bias)
            tmp_list = []
            for _ in range(self.N):
                tmp_dict = get_gradients(self.game, self.optimizer, batch)
                tmp_list.append(tmp_dict)
            batches_gradient_Nforward[n_batches] = tmp_list

            # Get sender RF gradients  (to compute bias)
            ## Update rf_game with game parameters (same for rf_optimizer)
            self.rf_game.sender.load_state_dict(self.game.sender.state_dict())
            # TODO: hard coded here
            #compatible_state_dict = OrderedDict()
            #for key, values in self.game.receiver.state_dict().items():
            #    compatible_state_dict[f'agent.{key}'] = values

            compatible_state_dict = self.game.receiver.state_dict()
            self.rf_game.receiver.load_state_dict(compatible_state_dict)
            self.rf_optimizer.load_state_dict(self.optimizer.state_dict())
            ## Get RF gradients
            tmp_list = []
            for _ in range(self.N):
                tmp_dict = get_gradients(self.rf_game, self.rf_optimizer, batch)
                tmp_list.append(tmp_dict)
            RF_batches_gradient_Nforward[n_batches] = tmp_list

            # Get the gradient to backprob (to compute variance)
            self.optimizer.zero_grad()
            optimized_loss, rest = self.game(*batch)
            mean_rest = _add_dicts(mean_rest, rest)
            optimized_loss.backward()
            forward1_dict = {}
            for name, param in self.game.sender.named_parameters():
                if param.requires_grad:
                    forward1_dict[name] = param.grad.cpu().numpy()
            batches_gradient_1forward[n_batches] = forward1_dict
            # Backprob
            self.optimizer.step()

            mean_loss += optimized_loss
            n_batches += 1

        mean_loss /= n_batches
        mean_rest = _div_dict(mean_rest, n_batches)
        return mean_loss.item(), mean_rest, RF_batches_gradient_Nforward, batches_gradient_Nforward, batches_gradient_1forward

    def train(self, n_epochs):
        for callback in self.callbacks:
            callback.on_train_begin(self)

        epoch_gradient  = {}
        for epoch in range(self.start_epoch, n_epochs):
            for callback in self.callbacks:
                callback.on_epoch_begin()

            train_loss, train_rest, rf_gradients_Nsamples, gradients_Nsamples, gradients_1sample = self.train_epoch(epoch)
            epoch_gradient[epoch] = {'RFNsamples': rf_gradients_Nsamples, 'Nsamples': gradients_Nsamples, '1sample': gradients_1sample}

            for callback in self.callbacks:
                callback.on_epoch_end(train_loss, train_rest)

            if self.validation_data is not None and self.validation_freq > 0 and epoch % self.validation_freq == 0:
                for callback in self.callbacks:
                    callback.on_test_begin()
                validation_loss, rest = self.eval()
                for callback in self.callbacks:
                    callback.on_test_end(validation_loss, rest)

            if self.should_stop:
                break
    
        # Save all gradients
        path = self.checkpoint_path / f'gradients.pkl'
        pickle.dump(epoch_gradient, open(path, 'wb'))   

        for callback in self.callbacks:
            callback.on_train_end()
