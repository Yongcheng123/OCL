"""
This module provides the `TrainModel` class and supporting methods.
"""
import logging
import math
import os
import shutil
from time import time

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau

from .utils import initialize_logger
from .utils import load_model_from_state_dict
from .utils import PerformanceMetrics

logger = logging.getLogger("selene")


def _metrics_logger(name, out_filepath):
    logger = logging.getLogger("{0}".format(name))
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    file_handle = logging.FileHandler(
        os.path.join(out_filepath, "{0}.txt".format(name)))
    file_handle.setFormatter(formatter)
    logger.addHandler(file_handle)
    return logger


class TrainModel(object):
    """
    This class ties together the various objects and methods needed to
    train and validate a model.

    Parameters
    ----------
    model : torch.nn.Module
        The model to train.
    data_sampler : selene_sdk.samplers.Sampler
        The example generator.
    loss_criterion : torch.nn._Loss
        The loss function to optimize.
    optimizer_class : torch.optim.Optimizer
        The optimizer to minimize loss with.
    optimizer_kwargs : dict
        The dictionary of keyword arguments to pass to the optimizer's
        constructor.
    batch_size : int
        Specify the batch size to process examples. Should be a power of 2.
    max_steps : int
        The maximum number of mini-batches to iterate over.
    report_stats_every_n_steps : int
        The frequency with which to report summary statistics.
    output_dir : str
        The output directory to save model checkpoints and logs in.
    save_checkpoint_every_n_steps : int or None, optional
        Default is 1000. If None, set to the same value as
        `report_stats_every_n_steps`
    n_validation_samples : int or None, optional
        Default is `None`. Specify the number of validation samples in the
        validation set. If `n_validation_samples` is `None` and the data sampler
        used is the `selene_sdk.samplers.IntervalsSampler` or
        `selene_sdk.samplers.RandomSampler`, we will retrieve 32000
        validation samples. If `None` and using
        `selene_sdk.samplers.MatFileSampler`, we will use all
        validation samples in the matrix.
    n_test_samples : int or None, optional
        Default is `None`. Specify the number of test samples in the test set.
        If `n_test_samples` is `None` and the data sampler used is the
        `selene_sdk.samplers.IntervalsSampler`, the size of the test set is
        the number of test intervals we can sample from. If
        `None` and using `selene_sdk.samplers.RandomSampler`, the size is
        :math:`\\frac{N}{20}`, where :math:`N` is the number of possible
        positions in the test partition of the genome. If  `None` and
        using `selene_sdk.samplers.MatFileSampler`, we will use all
        test samples in the matrix.
    cpu_n_threads : int, optional
        Default is 32.
    use_cuda : bool, optional
        Default is `False`. Specify whether CUDA is available for torch
        to use during training.
    data_parallel : bool, optional
        Default is `False`. Specify whether multiple GPUs are available
        for torch to use during training.
    checkpoint_resume : str or None, optional
        Default is `None`. If `checkpoint_resume` is not None, it should be the
        path to a model file generated by `torch.save` that can now be read
        using `torch.load`.

    Attributes
    ----------
    model : torch.nn.Module
        The model to train.
    sampler : selene_sdk.samplers.Sampler
        The example generator.
    loss_criterion : torch.nn._Loss
        The loss function to optimize.
    optimizer_class : torch.optim.Optimizer
        The optimizer to minimize loss with.
    batch_size : int
        The size of the mini-batch to use during training.
    max_steps : int
        The maximum number of mini-batches to iterate over.
    nth_step_report_stats : int
        The frequency with which to report summary statistics.
    nth_step_save_checkpoint : int
        The frequency with which to save a model checkpoint.
    use_cuda : bool
        If `True`, use a CUDA-enabled GPU. If `False`, use the CPU.
    data_parallel : bool
        Whether to use multiple GPUs or not.
    output_dir : str
        The directory to save model checkpoints and logs.
    training_loss : list(float)
        The current training loss.

    """

    def __init__(self,
                 model,
                 data_sampler,
                 loss_criterion,
                 optimizer_class,
                 optimizer_kwargs,
                 batch_size,
                 max_steps,
                 report_stats_every_n_steps,
                 output_dir,
                 save_checkpoint_every_n_steps=1000,
                 report_gt_feature_n_positives=10,
                 n_validation_samples=None,
                 n_test_samples=None,
                 cpu_n_threads=32,
                 use_cuda=False,
                 data_parallel=False,
                 logging_verbosity=2,
                 checkpoint_resume=None):
        """
        Constructs a new `TrainModel` object.
        """
        self.model = model
        self.sampler = data_sampler
        self.criterion = loss_criterion
        self.optimizer = optimizer_class(
            self.model.parameters(), **optimizer_kwargs)

        self.batch_size = batch_size
        self.max_steps = max_steps
        self.nth_step_report_stats = report_stats_every_n_steps
        self.nth_step_save_checkpoint = None
        if not save_checkpoint_every_n_steps:
            self.nth_step_save_checkpoint = report_stats_every_n_steps
        else:
            self.nth_step_save_checkpoint = save_checkpoint_every_n_steps

        torch.set_num_threads(cpu_n_threads)

        self.use_cuda = use_cuda
        self.data_parallel = data_parallel

        if self.data_parallel:
            self.model = nn.DataParallel(model)
            logger.debug("Wrapped model in DataParallel")

        if self.use_cuda:
            self.model.cuda()
            self.criterion.cuda()
            logger.debug("Set modules to use CUDA")

        os.makedirs(output_dir, exist_ok=True)
        self.output_dir = output_dir

        initialize_logger(
            os.path.join(self.output_dir, "{0}.log".format(__name__)),
            verbosity=logging_verbosity)

        self._create_validation_set(n_samples=n_validation_samples)
        # TODO: Only `selene_sdk.samplers.OnlineSampler` and sub-classes have `get_feature_from_index`.
        # So, what should be done to allow more general sampling methods?
        self._validation_metrics = PerformanceMetrics(
            self.sampler.get_feature_from_index,
            report_gt_feature_n_positives=report_gt_feature_n_positives)

        if "test" in self.sampler.modes:
            self._create_test_set(n_samples=n_test_samples)
            self._test_metrics = PerformanceMetrics(
                self.sampler.get_feature_from_index,
                report_gt_feature_n_positives=report_gt_feature_n_positives)

        self._start_step = 0
        self._min_loss = float("inf") # TODO: Should this be set when it is used later? Would need to if we want to train model 2x in one run.
        if checkpoint_resume is not None:
            checkpoint = torch.load(
                checkpoint_resume,
                map_location=lambda storage, location: storage)

            self.model = load_model_from_state_dict(
                checkpoint["state_dict"], self.model)

            self._start_step = checkpoint["step"]
            if self._start_step >= self.max_step:
                self.max_step += self._start_step

            self._min_loss = checkpoint["min_loss"]
            self.optimizer.load_state_dict(
                checkpoint["optimizer"])
            if self.use_cuda:
                for state in self.optimizer.state.values():
                    for k, v in state.items():
                        if isinstance(v, torch.Tensor):
                            state[k] = v.cuda()

            logger.info(
                ("Resuming from checkpoint: step {0}, min loss {1}").format(
                    self._start_step, self._min_loss))

        self._train_logger = _metrics_logger(
                "{0}.train".format(__name__), self.output_dir)
        self._validation_logger = _metrics_logger(
                "{0}.validation".format(__name__), self.output_dir)

        self._train_logger.info("loss")
        # TODO: this makes the assumption that all models will report ROC AUC,
        # which is not the case.
        self._validation_logger.info("loss\troc_auc")

    def _create_validation_set(self, n_samples=None):
        """
        Generates the set of validation examples.

        Parameters
        ----------
        n_samples : int or None, optional
            Default is `None`. The size of the validation set. If `None`,
            will use all validation examples in the sampler.

        """
        t_i = time()
        self._validation_data, self._all_validation_targets = \
            self.sampler.get_validation_set(
                self.batch_size, n_samples=n_samples)
        t_f = time()
        # TODO: Correct the # of examples and batches so that they reflect the actual #, not estimates.
        logger.info(("{0} s to load {1} validation examples ({2} validation "
                     "batches) to evaluate after each training step.").format(
                      t_f - t_i,
                      len(self._validation_data) * self.batch_size,
                      len(self._validation_data)))

    # TODO: Determine if we can somehow combine testing and validation set creation methods.
    def _create_test_set(self, n_samples=None):
        """
        Generates the set of test examples.

        Parameters
        ----------
        n_samples : int or None, optional
            Default is `None`. The size of the test set to generate. If
            `None`, will use all test examples in the sampler.

        """
        t_i = time()
        self._test_data, self._all_test_targets = \
            self.sampler.get_test_set(
                self.batch_size, n_samples=n_samples)
        t_f = time()
        # TODO: Correct the # of examples and batches so that they reflect the actual #, not estimates.
        logger.info(("{0} s to load {1} test examples ({2} test batches) "
                     "to evaluate after all training steps.").format(
                      t_f - t_i,
                      len(self._test_data) * self.batch_size,
                      len(self._test_data)))
        np.savez_compressed(
            os.path.join(self.output_dir, "test_targets.npz"),
            data=self._all_test_targets)

    def _get_batch(self):
        """
        Fetches a mini-batch of examples

        Returns
        -------
        tuple(numpy.ndarray, numpy.ndarray)
            A tuple containing the examples and targets.

        """
        t_i_sampling = time()
        batch_sequences, batch_targets = self.sampler.sample(
            batch_size=self.batch_size)
        t_f_sampling = time()
        logger.debug(
            ("[BATCH] Time to sample {0} examples: {1} s.").format(
                 self.batch_size,
                 t_f_sampling - t_i_sampling))
        return (batch_sequences, batch_targets)

    def train_and_validate(self):
        """
        Trains the model and measures validation performance.

        """
        logger.info(
            ("[TRAIN] max_steps: {0}, batch_size: {1}").format(
                self.max_steps, self.batch_size))

        min_loss = self._min_loss
        scheduler = ReduceLROnPlateau(
            self.optimizer, 'max', patience=16, verbose=True,
            factor=0.8)
        for step in range(self._start_step, self.max_steps):
            train_loss = self.train()

            # TODO: Should we have some way to report training stats without running validation?
            if step and step % self.nth_step_report_stats == 0:
                valid_scores = self.validate()
                validation_loss = valid_scores["loss"]
                self._train_logger.info(train_loss)
                # TODO: check if "roc_auc" is a key in `valid_scores`?
                if valid_scores["roc_auc"]:
                    validation_roc_auc = valid_scores["roc_auc"]
                    self._validation_logger.info(
                        "{0}\t{1}".format(validation_loss,
                                          validation_roc_auc))
                    scheduler.step(
                        math.ceil(validation_roc_auc * 1000.0) / 1000.0)
                else:
                    self._validation_logger.info("{0}\tNA".format(
                        validation_loss))

                is_best = validation_loss < min_loss
                min_loss = min(validation_loss, min_loss)
                self._save_checkpoint({
                    "step": step,
                    "arch": self.model.__class__.__name__,
                    "state_dict": self.model.state_dict(),
                    "min_loss": min_loss,
                    "optimizer": self.optimizer.state_dict()}, is_best)
                logger.info(
                    ("[STATS] step={0}: "
                     "Training loss: {1}, validation loss: {2}.").format(
                        step, train_loss, validation_loss)) # Should training loss and validation loss be reported at the same line?
                # Logging training and validation on same line requires 2 parsers or more complex parser.
                # Separate logging of train/validate is just a grep for validation/train and then same parser.

            # TODO: Do we want to save a checkpoint at step == 0 (as is the case now)?
            # Should checkpoint saving occur before validation (if they both occur in the same step) or not?
            if step % self.nth_step_save_checkpoint == 0:
                self._save_checkpoint({
                    "step": step,
                    "arch": self.model.__class__.__name__,
                    "state_dict": self.model.state_dict(),
                    "min_loss": min_loss,
                    "optimizer": self.optimizer.state_dict()}, False)
        self.sampler.save_dataset_to_file("train", close_filehandle=True)

    def train(self):
        """
        Trains the model on a batch of data.

        Returns
        -------
        float
            The training loss.

        """
        self.model.train()
        self.sampler.set_mode("train")

        inputs, targets = self._get_batch()

        inputs = torch.Tensor(inputs)
        targets = torch.Tensor(targets)

        if self.use_cuda:
            inputs = inputs.cuda()
            targets = targets.cuda()

        inputs = Variable(inputs)
        targets = Variable(targets)

        predictions = self.model(inputs.transpose(1, 2))
        loss = self.criterion(predictions, targets)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return loss.item()

    def _evaluate_on_data(self, data_in_batches):
        """
        Makes predictions for some labeled input data.

        Parameters
        ----------
        data_in_batches : list(tuple(numpy.ndarray, numpy.ndarray))
            A list of tuples of the data, where the first element is
            the example, and the second element is the label.

        Returns
        -------
        tuple(float, list(numpy.ndarray))
            Returns the average loss, and the list of all predictions.

        """
        self.model.eval()

        batch_losses = []
        all_predictions = []

        for (inputs, targets) in data_in_batches:
            inputs = torch.Tensor(inputs)
            targets = torch.Tensor(targets)

            if self.use_cuda:
                inputs = inputs.cuda()
                targets = targets.cuda()

            with torch.no_grad():
                inputs = Variable(inputs)
                targets = Variable(targets)

                predictions = self.model(
                    inputs.transpose(1, 2))
                loss = self.criterion(predictions, targets)

                all_predictions.append(
                    predictions.data.cpu().numpy())

                batch_losses.append(loss.item())
        all_predictions = np.vstack(all_predictions)
        return np.average(batch_losses), all_predictions

    def validate(self):
        """
        Measures model validation performance.

        Returns
        -------
        dict
            A dictionary, where keys are the names of the loss metrics,
            and the values are the average value for that metric over
            the validation set.

        """
        average_loss, all_predictions = self._evaluate_on_data(
            self._validation_data)

        average_scores = self._validation_metrics.update(all_predictions,
                                                         self._all_validation_targets)

        # TODO: This results in validation loss being logged twice. Is that ideal?
        for name, score in average_scores.items():
            logger.debug("[STATS] average {0}: {1}".format(name, score))
            print("[VALIDATE] average {0}: {1}".format(name, score))

        average_scores["loss"] = average_loss
        return average_scores

    def evaluate(self):
        """
        Measures the model test performance.

        Returns
        -------
        dict
            A dictionary, where keys are the names of the loss metrics,
            and the values are the average value for that metric over
            the test set.

        """
        average_loss, all_predictions = self._evaluate_on_data(
            self._test_data)

        average_scores = self._test_metrics.update(all_predictions,
                                                   self._all_test_targets)
        np.savez_compressed(
            os.path.join(self.output_dir, "test_predictions.npz"),
            data=all_predictions)

        for name, score in average_scores.items():
            logger.debug("[STATS] average {0}: {1}".format(name, score))
            print("[TEST] average {0}: {1}".format(name, score))

        test_performance = os.path.join(
            self.output_dir, "test_performance.txt")
        feature_scores_dict = self._test_metrics.write_feature_scores_to_file(
            test_performance)

        average_scores["loss"] = average_loss

        self._test_metrics.visualize(
            all_predictions, self._all_test_targets, self.output_dir)

        return (average_scores, feature_scores_dict)

    def _save_checkpoint(self, state, is_best,
                         dir_path=None,
                         filename="checkpoint.pth.tar"):
        """
        Saves snapshot of the model state to file.

        Parameters
        ----------
        state : dict
            Information about the state of the model
        is_best : bool
            Is this the model's best performance so far?
        dir_path : str, optional
            Default is None. Will output file to the current working directory
            if no path to directory is specified.
        filename : str, optional
            Default is "checkpoint.pth.tar". Specify the checkpoint filename.

        Returns
        -------
        None

        """
        logger.info("[TRAIN] {0}: Saving model state to file.".format(
            state["step"]))
        cp_filepath = os.path.join(
            self.output_dir, filename)
        torch.save(state, cp_filepath)
        if is_best:
            best_filepath = os.path.join(
                self.output_dir,
                "best_model.pth.tar")
            shutil.copyfile(cp_filepath, best_filepath)
