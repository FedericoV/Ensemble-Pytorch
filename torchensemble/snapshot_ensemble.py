"""
  Snapshot ensemble generates many base estimators by enforcing a base
  estimator to converge to its local minima many times and save the
  model parameters at that point as a snapshot. The final prediction takes
  the average over predictions from all snapshot models.

  Reference:
      G. Huang, Y.-X. Li, G. Pleiss et al., Snapshot Ensemble: Train 1, and
      M for free, ICLR, 2017.
"""

import copy
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import LambdaLR

from ._base import BaseModule, torchensemble_model_doc
from . import utils


__author__ = ["Yi-Xuan Xu"]
__all__ = ["_BaseSnapshotEnsemble",
           "SnapshotEnsembleClassifier",
           "SnapshotEnsembleRegressor"]


__fit_doc = """
    Parameters
    ----------
    train_loader : torch.utils.data.DataLoader
        A :mod:`DataLoader` container that contains the training data.
    init_lr : float, default=1e-1
        The initial learning rate of the parameter optimizer. Snapshot
        ensemble will adjust the learning rate based on ``init_lr``,
        ``epochs``, and ``n_estimators`` automatically.
    weight_decay : float, default=5e-4
        The weight decay of the parameter optimizer.
    epochs : int, default=100
        The number of training epochs.
    optimizer : {"SGD", "Adam", "RMSprop"}, default="Adam"
        The type of parameter optimizer.
    log_interval : int, default=100
        The number of batches to wait before printting the training status.
    test_loader : torch.utils.data.DataLoader, default=None
        A :mod:`DataLoader` container that contains the evaluating data.

        - If ``None``, no validation is conducted after each training
          epoch.
        - If not ``None``, the ensemble will be evaluated on this
          dataloader after each training epoch.
    save_model : bool, default=True
        Whether to save the model.

        - If test_loader is ``None``, the ensemble containing
          ``n_estimators`` base estimators will be saved.
        - If test_loader is not ``None``, the ensemble with the best
          validation performance will be saved.
    save_dir : string, default=None
        Specify where to save the model.

        - If ``None``, the model will be saved in the current directory.
        - If not ``None``, the model will be saved in the specified
          directory: ``save_dir``.
"""


def _snapshot_ensemble_model_doc(header, item="fit"):
    """
    Decorator on obtaining documentation for different gradient boosting
    models.
    """
    def get_doc(item):
        """Return selected item"""
        __doc = {"fit": __fit_doc}
        return __doc[item]

    def adddoc(cls):
        doc = [header + "\n\n"]
        doc.extend(get_doc(item))
        cls.__doc__ = "".join(doc)
        return cls
    return adddoc


class _BaseSnapshotEnsemble(BaseModule):

    def __init__(self,
                 estimator,
                 n_estimators,
                 estimator_args=None,
                 cuda=True,
                 verbose=1):
        super(BaseModule, self).__init__()

        # Make sure estimator is not an instance
        if not isinstance(estimator, type):
            msg = ("The input argument `estimator` should be a class"
                   " inherited from nn.Module. Perhaps you have passed"
                   " an instance of that class into the ensemble.")
            raise RuntimeError(msg)

        self.base_estimator_ = estimator
        self.n_estimators = n_estimators
        self.estimator_args = estimator_args
        self.device = torch.device("cuda" if cuda else "cpu")
        self.verbose = verbose

        self.estimators_ = nn.ModuleList()

    def _validate_parameters(self,
                             init_lr,
                             lr_clip,
                             weight_decay,
                             epochs,
                             log_interval):
        """Validate hyper-parameters on training the ensemble."""

        if not init_lr > 0:
            msg = ("The initial learning rate of optimizer = {} should be"
                   " strictly positive.")
            raise ValueError(msg.format(init_lr))

        if lr_clip:
            if not (isinstance(lr_clip, list) or isinstance(lr_clip, tuple)):
                msg = "lr_clip should be a list or tuple with two elements."
                raise ValueError(msg)

            if len(lr_clip) != 2:
                msg = ("lr_clip should only have two elements, one for lower"
                       " bound, and another for upper bound.")
                raise ValueError(msg)

            if not lr_clip[0] < lr_clip[1]:
                msg = ("The first element = {} should be smaller than the"
                       " second element = {} in lr_clip.")
                raise ValueError(msg.format(lr_clip[0], lr_clip[1]))

        if not weight_decay >= 0:
            msg = "The weight decay of optimizer = {} should not be negative."
            raise ValueError(msg.format(weight_decay))

        if not epochs > 0:
            msg = ("The number of training epochs = {} should be strictly"
                   " positive.")
            raise ValueError(msg.format(epochs))

        if not log_interval > 0:
            msg = ("The number of batches to wait before printting the"
                   " training status should be strictly positive, but got {}"
                   " instead.")
            raise ValueError(msg.format(log_interval))

        if not epochs % self.n_estimators == 0:
            msg = ("The number of training epochs = {} should be a multiple"
                   " of n_estimators = {}.")
            raise ValueError(msg.format(epochs, self.n_estimators))

    def _forward(self, X):
        """
        Implementation on the internal data forwarding in snapshot ensemble.
        """
        batch_size = X.size()[0]
        output = torch.zeros(batch_size, self.n_outputs).to(self.device)

        # Average
        for estimator in self.estimators_:
            output += estimator(X) / len(self.estimators_)

        return output

    def _clip_lr(self, optimizer, lr_clip):
        """Clip the learning rate of the optimizer according to `lr_clip`."""
        if not lr_clip:
            return optimizer

        for param_group in optimizer.param_groups:
            if param_group["lr"] < lr_clip[0]:
                param_group["lr"] = lr_clip[0]
            if param_group["lr"] > lr_clip[1]:
                param_group["lr"] = lr_clip[1]

        return optimizer

    def _set_scheduler(self, optimizer, n_iters):
        """
        Set the learning rate scheduler for snapshot ensemble.
        Please refer to the equation (2) in original paper for details.
        """
        T_M = math.ceil(n_iters / self.n_estimators)
        lr_lambda = lambda iteration: 0.5 * (  # noqa: E731
            torch.cos(torch.tensor(math.pi * (iteration % T_M) / T_M)) + 1
        )
        scheduler = LambdaLR(optimizer, lr_lambda=lr_lambda)

        return scheduler


@torchensemble_model_doc(
    """Implementation on the SnapshotEnsembleClassifier.""", "model")
class SnapshotEnsembleClassifier(_BaseSnapshotEnsemble):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_classification = True

    @torchensemble_model_doc(
        """Implementation on the data forwarding in SnapshotEnsembleClassifier.""",  # noqa: E501
        "classifier_forward")
    def forward(self, X):
        proba = self._forward(X)

        return F.softmax(proba, dim=1)

    @_snapshot_ensemble_model_doc(
        """Implementation on the training stage of SnapshotEnsembleClassifier.""",  # noqa: E501
        "fit"
    )
    def fit(self,
            train_loader,
            init_lr=1e-1,
            lr_clip=None,
            weight_decay=5e-4,
            epochs=100,
            optimizer="Adam",
            log_interval=100,
            test_loader=None,
            save_model=True,
            save_dir=None):

        self.n_outputs = self._decide_n_outputs(train_loader,
                                                self.is_classification)
        self._validate_parameters(init_lr,
                                  lr_clip,
                                  weight_decay,
                                  epochs,
                                  log_interval)

        # Model used to generate snapshot ensembles
        estimator_ = self._make_estimator()

        # Optimizer and Scheduler
        optimizer = utils.set_optimizer(estimator_,
                                        optimizer,
                                        init_lr,
                                        weight_decay)

        scheduler = self._set_scheduler(optimizer, epochs * len(train_loader))

        estimator_.train()

        # Utils
        criterion = nn.CrossEntropyLoss()
        best_acc = 0.
        counter = 0  # the counter on generating snapshots
        n_iters_per_estimator = epochs * len(train_loader) // self.n_estimators

        # Training loop
        for epoch in range(epochs):
            for batch_idx, (data, target) in enumerate(train_loader):

                # Clip the learning rate
                optimizer = self._clip_lr(optimizer, lr_clip) 

                batch_size = data.size()[0]
                data, target = data.to(self.device), target.to(self.device)

                output = estimator_(data)
                loss = criterion(output, target)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # Print training status
                if batch_idx % log_interval == 0:
                    with torch.no_grad():
                        pred = output.data.max(1)[1]
                        correct = pred.eq(target.view(-1).data).sum()

                        if self.verbose > 0:
                            msg = ("{} lr: {:.5f} | Epoch: {:03d} | Batch:"
                                   " {:03d} | Loss: {:.5f} | Correct: "
                                   "{:d}/{:d}")
                            print(
                                msg.format(
                                    utils.ctime(),
                                    optimizer.param_groups[0]["lr"],
                                    epoch,
                                    batch_idx,
                                    loss,
                                    correct,
                                    batch_size
                                )
                            )

                # Snapshot ensemble updates the learning rate per iteration
                # instead of per epoch.
                scheduler.step()
                counter += 1

            if counter % n_iters_per_estimator == 0:
                # Generate and save the snapshot
                snapshot = copy.deepcopy(estimator_)
                self.estimators_.append(snapshot)

                if self.verbose > 0:
                    msg = "{} Generate the snapshot with index: {}"
                    print(msg.format(utils.ctime(), len(self.estimators_) - 1))

            # Validation after each snapshot being generated
            if test_loader and counter % n_iters_per_estimator == 0:
                with torch.no_grad():
                    correct = 0.
                    for batch_idx, (data, target) in enumerate(test_loader):
                        data, target = (data.to(self.device),
                                        target.to(self.device))
                        output = self.forward(data)
                        pred = output.data.max(1)[1]
                        correct += pred.eq(target.view(-1).data).sum()
                    acc = 100. * float(correct) / len(test_loader.dataset)

                    if acc > best_acc:
                        best_acc = acc
                        if save_model:
                            utils.save(self, save_dir, self.verbose)

                    if self.verbose > 0:
                        msg = ("{} n_estimators: {} | Validation Acc: {:.3f} %"
                               " | Historical Best: {:.3f} %")
                        print(msg.format(
                            utils.ctime(),
                            len(self.estimators_),
                            acc,
                            best_acc)
                        )

        if save_model and not test_loader:
            utils.save(self, save_dir, self.verbose)

    @torchensemble_model_doc(
        """Implementation on the evaluating stage of SnapshotEnsembleClassifier.""",  # noqa: E501
        "classifier_predict")
    def predict(self, test_loader):
        self.eval()
        correct = 0

        for batch_idx, (data, target) in enumerate(test_loader):
            data, target = data.to(self.device), target.to(self.device)
            output = self.forward(data)
            pred = output.data.max(1)[1]
            correct += pred.eq(target.view(-1).data).sum()

        accuracy = 100. * float(correct) / len(test_loader.dataset)

        return accuracy


@torchensemble_model_doc(
    """Implementation on the SnapshotEnsembleRegressor.""", "model")
class SnapshotEnsembleRegressor(_BaseSnapshotEnsemble):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_classification = False

    @torchensemble_model_doc(
        """Implementation on the data forwarding in SnapshotEnsembleRegressor.""",  # noqa: E501
        "regressor_forward")
    def forward(self, X):
        pred = self._forward(X)
        return pred

    @_snapshot_ensemble_model_doc(
        """Implementation on the training stage of SnapshotEnsembleRegressor.""",  # noqa: E501
        "fit"
    )
    def fit(self,
            train_loader,
            init_lr=1e-1,
            lr_clip=None,
            weight_decay=5e-4,
            epochs=100,
            optimizer="Adam",
            log_interval=100,
            test_loader=None,
            save_model=True,
            save_dir=None):

        self.n_outputs = self._decide_n_outputs(train_loader,
                                                self.is_classification)
        self._validate_parameters(init_lr,
                                  lr_clip,
                                  weight_decay,
                                  epochs,
                                  log_interval)

        # Model used to generate snapshot ensembles
        estimator_ = self._make_estimator()

        # Optimizer and Scheduler
        optimizer = utils.set_optimizer(estimator_,
                                        optimizer,
                                        init_lr,
                                        weight_decay)

        scheduler = self._set_scheduler(optimizer, epochs * len(train_loader))

        estimator_.train()

        # Utils
        criterion = nn.MSELoss()
        best_mse = float("inf")
        counter = 0  # the counter on generating snapshots
        n_iters_per_estimator = epochs * len(train_loader) // self.n_estimators

        # Training loop
        for epoch in range(epochs):
            for batch_idx, (data, target) in enumerate(train_loader):

                # Clip the learning rate
                optimizer = self._clip_lr(optimizer, lr_clip)

                data, target = data.to(self.device), target.to(self.device)

                output = estimator_(data)
                loss = criterion(output, target)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # Print training status
                if batch_idx % log_interval == 0:
                    with torch.no_grad():
                        msg = ("{} lr: {:.5f} | Epoch: {:03d} | Batch: {:03d}"
                               " | Loss: {:.5f}")
                        print(
                            msg.format(
                                utils.ctime(),
                                optimizer.param_groups[0]["lr"],
                                epoch,
                                batch_idx,
                                loss)
                        )

                # Snapshot ensemble updates the learning rate per iteration
                # instead of per epoch.
                scheduler.step()
                counter += 1

            if counter % n_iters_per_estimator == 0:
                # Generate and save the snapshot
                snapshot = copy.deepcopy(estimator_)
                self.estimators_.append(snapshot)

                if self.verbose > 0:
                    msg = "{} Generate the snapshot with index: {}"
                    print(msg.format(utils.ctime(), len(self.estimators_) - 1))

            # Validation after each snapshot being generated
            if test_loader and counter % n_iters_per_estimator == 0:
                with torch.no_grad():
                    mse = 0.
                    for batch_idx, (data, target) in enumerate(test_loader):
                        data, target = (data.to(self.device),
                                        target.to(self.device))
                        output = self.forward(data)
                        mse += criterion(output, target)
                    mse /= len(test_loader)

                    if mse < best_mse:
                        best_mse = mse
                        if save_model:
                            utils.save(self, save_dir, self.verbose)

                    if self.verbose > 0:
                        msg = ("{} n_estimators: {} | Validation MSE: {:.5f} |"
                               " Historical Best: {:.5f}")
                        print(msg.format(
                            utils.ctime(),
                            len(self.estimators_),
                            mse,
                            best_mse)
                        )

        if save_model and not test_loader:
            utils.save(self, save_dir, self.verbose)

    @torchensemble_model_doc(
        """Implementation on the evaluating stage of SnapshotEnsembleRegressor.""",  # noqa: E501
        "regressor_predict")
    def predict(self, test_loader):
        self.eval()
        mse = 0.
        criterion = nn.MSELoss()

        for batch_idx, (data, target) in enumerate(test_loader):
            data, target = data.to(self.device), target.to(self.device)
            output = self.forward(data)

            mse += criterion(output, target)

        return mse / len(test_loader)
