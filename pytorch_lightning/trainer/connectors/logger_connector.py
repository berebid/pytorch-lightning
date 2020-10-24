# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from enum import Enum
import torch
from pytorch_lightning.core import memory
from pytorch_lightning.loggers import TensorBoardLogger, LoggerCollection
from pytorch_lightning.utilities import flatten_dict
from pytorch_lightning.utilities.model_utils import is_overridden
from pytorch_lightning.core.step_result import EvalResult, Result
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pprint import pprint
from typing import Iterable, Union
from copy import deepcopy
from collections import defaultdict, ChainMap

class LoggerStages(Enum):
    TRAIN = "train"
    VAL = "validation"
    TEST = "test"
    ALL = "all"


class HookResults:

    def __init__(self, current_hook_fx_name):
        self._current_hook_fx_name = current_hook_fx_name
        self._internals = defaultdict(list)

    def append(self, result, dataloader_idx=None, split_idx=None):
        # TODO handle split_idx
        if dataloader_idx is None:
            dataloader_idx = 0
        self._internals[dataloader_idx].append(result)

    def __repr__(self):
        return self._internals.__repr__()

class EpochLoopResult:

    def __init__(self, trainer, stage):
        self.trainer = trainer
        self._stage = stage
        self._internals = {}
        self._dataloader_idx = None
        self._split_idx = None

    def cache_result(self):
        model_ref = self.trainer.get_model()

        # extract hook information
        hook_result = model_ref._results
        current_hook_fx_name = model_ref._current_hook_fx_name

        if current_hook_fx_name not in self._internals:
            self._internals[current_hook_fx_name] = HookResults(current_hook_fx_name)
        
        self._internals[current_hook_fx_name].append(hook_result, dataloader_idx=self._dataloader_idx, split_idx=self._split_idx)

    def __repr__(self):
        return f"{self.__class__.__name__}(stage={self._stage}, internals={self._internals})"

    def log_training_step_metrics(self, opt_closure_result):

        # decide which metrics to log (results vs dict return)
        using_results_obj = isinstance(opt_closure_result.training_step_output, Result)

        if using_results_obj:
            metrics_to_log = opt_closure_result.training_step_output.get_batch_log_metrics(
                include_forked_originals=False
            )
            step_pbar_metrics = opt_closure_result.training_step_output.get_batch_pbar_metrics(
                include_forked_originals=False
            )
            forked_metrics = opt_closure_result.training_step_output.get_forked_metrics()
            callback_metrics.update(forked_metrics)

        else:
            metrics_to_log = opt_closure_result.training_step_output.log_metrics
            step_pbar_metrics = opt_closure_result.training_step_output.pbar_on_batch_end

        # track batch log metrics
        batch_log_metrics.append(metrics_to_log)

        # add initially computed step metrics.
        cache_internal_batch_pbar_metrics = self.trainer.logger_connector.cached_metrics("train").get_as_dict(
            "before_on_batch_start", "batch_pbar_metrics")
        if len(cache_internal_batch_pbar_metrics) > 0:
            self.trainer.logger_connector.add_progress_bar_metrics(cache_internal_batch_pbar_metrics)
            self.trainer.logger_connector.callback_metrics.update(cache_internal_batch_pbar_metrics)

        # track progress bar metrics
        if len(step_pbar_metrics) > 0:
            self.trainer.logger_connector.add_progress_bar_metrics(step_pbar_metrics)
            self.trainer.logger_connector.callback_metrics.update(step_pbar_metrics)

        batch_callback_metrics.append(callback_metrics)

    def reset(self):
        self._internals = {}

class BatchSplitIdxManager: 
    def __init__(self, trainer, stage): 
        self.trainer = trainer
        self.stage = stage
          
    def __enter__(self): 
        self.trainer.split_idx = self.split_idx
        self.trainer.logger_connector._cached_results[self.stage].split_idx = self.split_idx
      
    def __exit__(self, exc_type, exc_value, exc_traceback): 
        self.trainer.logger_connector._cached_results[self.stage].split_idx = None

class LoggerConnector:

    __lookup_stages = {"0": "test", "1": "val", "True": "test", "False": "val"}

    def __init__(self, trainer):
        self.trainer = trainer
        self.callback_metrics = {}
        self.logged_metrics = {}
        self.progress_bar_metrics = {}
        self.eval_loop_results = []
        self._current_stage = None
        self.__stages  = sorted([s.value for s in LoggerStages])
        self._cached_results = {stage: EpochLoopResult(trainer, stage) for stage in self.__stages}
        self._split_idx_manager = {stage: BatchSplitIdxManager(trainer, stage) for stage in self.__stages[1:]}

    def _determine_stage_from_hook_name(self, hook_name: str = None) -> str:
        if hook_name is None:
            model_ref = self.trainer.get_model()
            hook_name = model_ref._current_hook_fx_name
        if LoggerStages.TRAIN.value in hook_name:
            return LoggerStages.TRAIN.value
        elif LoggerStages.VAL.value in hook_name:
            return LoggerStages.VAL.value
        elif LoggerStages.TEST.value in hook_name:
            return LoggerStages.TEST.value
        else:
            return LoggerStages.ALL.value

    def _determine_stage(self, stage_or_testing: Union[str, bool]) -> str:
        stage_or_testing = str(stage_or_testing)
        stages = self.__stages[1:] # exclude all
        if stage_or_testing in stages:
            return stage_or_testing
        if stage_or_testing in self.__lookup_stages:
            # Acces using trainer.testing
            return self.__lookup_stages[stage_or_testing]
        raise MisconfigurationException(
            f"Provide stage_or_testing {stage_or_testing} doesn't belong either to {stages}"
            f" or {self.__lookup_stages.keys()}"
        )

    def split_idx_manager(self, stage_or_testing: Union[str, bool], split_idx: int) -> BatchSplitIdxManager:
        stage = self._determine_stage(stage_or_testing)
        self._current_stage = stage
        # used to access it in __enter__
        self._split_idx_manager[stage].split_idx = split_idx
        return self._split_idx_manager[stage]
    
    def __enter__(self): 
        return self
      
    def __exit__(self, exc_type, exc_value, exc_traceback): 
        pass

    def capture_logging(self) -> Union[EpochLoopResult, None]:
        stage = self._determine_stage_from_hook_name()
        self._cached_results[stage].cache_result()

    def on_trainer_init(self, logger, flush_logs_every_n_steps, log_every_n_steps):
        # logging
        self.configure_logger(logger)
        # todo: IDE is complaining, these shall be initialized in the Trainer init at leas as placeholders
        #  and assign here the desired value
        self.trainer.flush_logs_every_n_steps = flush_logs_every_n_steps
        self.trainer.log_every_n_steps = log_every_n_steps

    def configure_logger(self, logger):
        if logger is True:
            version = os.environ.get('PL_EXP_VERSION', self.trainer.slurm_job_id)

            # default logger
            self.trainer.logger = TensorBoardLogger(
                save_dir=self.trainer.default_root_dir,
                version=version,
                name='lightning_logs'
            )
        elif logger is False:
            self.trainer.logger = None
        else:
            if isinstance(logger, Iterable):
                self.trainer.logger = LoggerCollection(logger)
            else:
                self.trainer.logger = logger

    def log_metrics(self, metrics, grad_norm_dic, step=None):
        """Logs the metric dict passed in.
        If `step` parameter is None and `step` key is presented is metrics,
        uses metrics["step"] as a step

        Args:
            metrics (dict): Metric values
            grad_norm_dic (dict): Gradient norms
            step (int): Step for which metrics should be logged. Default value corresponds to `self.global_step`
        """
        # add gpu memory
        if self.trainer.on_gpu and self.trainer.log_gpu_memory:
            mem_map = memory.get_memory_profile(self.trainer.log_gpu_memory)
            metrics.update(mem_map)

        # add norms
        metrics.update(grad_norm_dic)

        # turn all tensors to scalars
        scalar_metrics = self.trainer.metrics_to_scalars(metrics)

        if "step" in scalar_metrics and step is None:
            step = scalar_metrics.pop("step")

        elif step is None:
            # added metrics by Lightning for convenience
            scalar_metrics['epoch'] = self.trainer.current_epoch
            step = self.trainer.global_step

        # log actual metrics
        if self.trainer.logger is not None:
            if self.trainer.is_global_zero:
                self.trainer.logger.agg_and_log_metrics(scalar_metrics, step=step)
                self.trainer.logger.save()

            # track the logged metrics
            self.logged_metrics.update(scalar_metrics)
            self.trainer.dev_debugger.track_logged_metrics_history(scalar_metrics)

    def add_progress_bar_metrics(self, metrics):
        for k, v in metrics.items():
            if isinstance(v, torch.Tensor):
                v = v.item()

            self.progress_bar_metrics[k] = v

        self.trainer.dev_debugger.track_pbar_metrics_history(metrics)

    def before_on_evaluation_epoch_end(self, deprecated_eval_results, epoch_logs, using_eval_result, test_mode):
        self._track_callback_metrics(deprecated_eval_results, using_eval_result)

        metrics_to_log = self.cached_metrics(self.trainer.testing)\
            .get_as_list("before_on_batch_start", "epoch_log_metrics")
        self._track_callback_metrics_1_0(epoch_logs, metrics_to_log, reduce_on_epoch=True)
        # TODO: deprecate parts of this for 1.0 (when removing results)
        self.__process_eval_epoch_end_results_and_log_legacy(deprecated_eval_results, test_mode)

    def _get_evaluate_epoch_results(self, test_mode):
        # log results of test
        if test_mode and self.trainer.is_global_zero and self.trainer.verbose_test:
            print('-' * 80)
            for result_idx, results in enumerate(self.eval_loop_results):
                print(f'DATALOADER:{result_idx} TEST RESULTS')
                pprint(results)
                print('-' * 80)

        if self.trainer.testing:
            callback_metrics = deepcopy(self.callback_metrics)
            if self.trainer.dev_debugger.enabled:
                callback_metrics.pop("debug_epoch")
            self.eval_loop_results.append(callback_metrics)
            results = [dict(ChainMap(*self.eval_loop_results))]
        else:
            results = self.eval_loop_results

        # clear mem
        self.eval_loop_results = []
        return results

    def track_metrics_on_evaluation_epoch_start(self, logs, metrics_to_log=[]):
        batch_logger_metrics = logs.get_batch_log_metrics()
        if len(batch_logger_metrics) > 0:
            metrics_to_log.append(batch_logger_metrics)

    def _track_callback_metrics_1_0(self, logs, metrics_to_log=[], reduce_on_epoch=False):
        step_metrics = self.trainer.evaluation_loop.step_metrics

        num_loaders = len(step_metrics)

        # clear mem
        self.trainer.evaluation_loop.step_metrics = []

        if self.trainer.running_sanity_check:
            return

        # ---------------------------
        # UPDATE EPOCH LOGGED METRICS
        # ---------------------------
        # (ie: in methods at the val_epoch_end level)
        # union the epoch logs with whatever was returned from loaders and reduced
        epoch_logger_metrics = logs.get_epoch_log_metrics()
        epoch_pbar_metrics = logs.get_epoch_pbar_metrics()

        self.logged_metrics.update(epoch_logger_metrics)
        self.add_progress_bar_metrics(epoch_pbar_metrics)

        # enable the metrics to be monitored
        self.callback_metrics.update(epoch_logger_metrics)
        self.callback_metrics.update(epoch_pbar_metrics)

        if len(epoch_logger_metrics) > 0:
            metrics_to_log.append(epoch_logger_metrics)

        # --------------------------------
        # UPDATE  METRICS PER DATALOADER
        # --------------------------------
        # each dataloader aggregated metrics
        # now we log all of them
        if reduce_on_epoch:
            for dl_idx, dl_metrics in enumerate(step_metrics):
                if len(dl_metrics) == 0:
                    continue

                reduced_epoch_metrics = dl_metrics[0].__class__.reduce_on_epoch_end(dl_metrics)
                logger_metrics = reduced_epoch_metrics.get_epoch_log_metrics()
                pbar_metrics = reduced_epoch_metrics.get_epoch_pbar_metrics()
                forked_metrics = reduced_epoch_metrics.get_forked_metrics()

                # track the metrics
                self.logged_metrics.update(logger_metrics)
                self.add_progress_bar_metrics(pbar_metrics)

                # enable the metrics to be monitored
                self.callback_metrics.update(logger_metrics)
                self.callback_metrics.update(pbar_metrics)

                # forked metrics were dropped, enable them for callbacks
                self.callback_metrics.update(forked_metrics)

                # track the final results for the dataloader
                self.add_to_eval_loop_results(dl_idx)

                # actually log
                if len(logger_metrics) > 0:
                    metrics_to_log.append(logger_metrics)

    def add_to_eval_loop_results(self, dl_idx):
        callback_metrics = deepcopy(self.callback_metrics)
        for key in list(callback_metrics.keys()):
            if "/dataloader_idx_" in key:
                dl_idx_in_key = int(key.split("_")[-1])
                # remove dl_idx from self.callback_metrics not belonging to this dataset.
                if dl_idx_in_key != dl_idx:
                    del callback_metrics[key]
        self.eval_loop_results.append(callback_metrics)

    def log_epoch_metrics_on_evaluation_end(self, metrics_to_log):
        metrics_to_log = dict(ChainMap(*metrics_to_log))

        if len(metrics_to_log) > 0:
            self.log_metrics(metrics_to_log, {})

    def _track_callback_metrics(self, eval_results, using_eval_result):
        if (
                len(eval_results) > 0 and
                (eval_results[0] is None or not isinstance(eval_results[0], Result))
        ):
            return

        if using_eval_result:
            if isinstance(eval_results, list):
                for eval_result in eval_results:
                    self.trainer.logger_connector.callback_metrics.update(eval_result.callback_metrics)
            else:
                self.trainer.logger_connector.callback_metrics.update(eval_results.callback_metrics)
        else:
            if isinstance(eval_results, list):
                for eval_result in eval_results:
                    # with a scalar return, auto set it to "val_loss" for callbacks
                    if isinstance(eval_result, torch.Tensor):
                        flat = {'val_loss': eval_result}
                    elif isinstance(eval_result, dict):
                        flat = flatten_dict(eval_result)

                    # removing val_loss magic word to map to checkpoint + ES callback
                    if 'val_loss' in flat:
                        flat['checkpoint_on'] = flat['val_loss']
                        flat['early_stop_on'] = flat['val_loss']
                    self.trainer.logger_connector.callback_metrics.update(flat)
            else:
                # with a scalar return, auto set it to "val_loss" for callbacks
                if isinstance(eval_results, torch.Tensor):
                    flat = {'val_loss': eval_results}
                else:
                    flat = flatten_dict(eval_results)

                # removing val_loss magic word to map to checkpoint + ES callback
                if 'val_loss' in flat:
                    flat['checkpoint_on'] = flat['val_loss']
                    flat['early_stop_on'] = flat['val_loss']
                self.trainer.logger_connector.callback_metrics.update(flat)

    def __process_eval_epoch_end_results_and_log_legacy(self, eval_results, test_mode):
        if self.trainer.running_sanity_check:
            return

        if eval_results is not None and len(eval_results) > 0:

            # in eval, the user may return something at every validation step without final reduction
            if not isinstance(eval_results, list):
                eval_results = [eval_results]

            for result_idx, result in enumerate(eval_results):
                if isinstance(result, EvalResult):
                    prog_bar_metrics = result.epoch_pbar_metrics
                    log_metrics = result.epoch_log_metrics
                    callback_metrics = result.callback_metrics

                    # in testing we don't need the callback metrics
                    if test_mode:
                        callback_metrics = {}
                else:
                    _, prog_bar_metrics, log_metrics, callback_metrics, _ = self.trainer.process_dict_result(result)

                # eval loop returns all metrics
                dataloader_result_metrics = {**prog_bar_metrics, **log_metrics, **callback_metrics}

                # add metrics to prog bar
                self.trainer.logger_connector.add_progress_bar_metrics(prog_bar_metrics)

                # log metrics
                if len(log_metrics) > 0:
                    self.trainer.logger_connector.log_metrics(log_metrics, {})

                # track metrics for callbacks (all prog bar, logged and callback metrics)
                self.trainer.logger_connector.callback_metrics.update(callback_metrics)
                self.trainer.logger_connector.callback_metrics.update(log_metrics)
                self.trainer.logger_connector.callback_metrics.update(prog_bar_metrics)

                if len(dataloader_result_metrics) > 0:
                    self.eval_loop_results.append(dataloader_result_metrics)

    def on_train_epoch_end(self, epoch_output):
        pass

    def log_train_epoch_end_metrics(self,
                                    epoch_output,
                                    checkpoint_accumulator,
                                    early_stopping_accumulator,
                                    num_optimizers):
        # epoch output is a list. Each item in that list has all the outputs per optimizer
        # epoch_output[optimizer_idx][training_step_idx][tbptt_index]
        # remember that not using truncated backprop is equivalent with truncated back prop of len(1)

        model = self.trainer.get_model()

        epoch_callback_metrics = {}

        # -----------------------
        # Calculate epoch callback values if given
        # -----------------------
        if checkpoint_accumulator.num_values > 0:
            epoch_callback_metrics['checkpoint_on'] = checkpoint_accumulator.mean()

        if early_stopping_accumulator.num_values > 0:
            epoch_callback_metrics['early_stop_on'] = early_stopping_accumulator.mean()

        # ------------------------
        # determine if using a result obj
        # ------------------------
        # [optimizer_idx][training_step_idx][tbptt_index]
        opt_idx_outputs = epoch_output[0]

        # TODO: deprecate 1.0
        try:
            sample_obj = opt_idx_outputs[0][0] if isinstance(opt_idx_outputs[0], list) else opt_idx_outputs[0]
            is_result_obj = len(epoch_output) > 0 and isinstance(sample_obj, Result)
            is_1_0_result = is_result_obj and 'extra' in sample_obj
        except IndexError as e:
            is_result_obj = False
            is_1_0_result = False

        # ------------------
        # NEW 1.0.0 PATH
        # ------------------
        if is_1_0_result:
            # lightning module hook
            epoch_end_log_result = self.training_epoch_end(model, epoch_output, num_optimizers)

            # log/aggregate metrics automatically
            epoch_log_metrics, epoch_progress_bar_metrics = self.__auto_reduce_results_on_epoch_end(epoch_output)
            epoch_log_metrics.update(epoch_end_log_result.get_epoch_log_metrics())
            epoch_progress_bar_metrics.update(epoch_end_log_result.get_epoch_pbar_metrics())

            cache_internal_epoch_log_metrics = self.cached_metrics("train")\
                .get_as_dict("after_on_batch_end", "epoch_log_metrics")
            epoch_log_metrics.update(cache_internal_epoch_log_metrics)

            cache_internal_epoch_pbar_metrics = self.cached_metrics("train")\
                .get_as_dict("after_on_batch_end", "epoch_pbar_metrics")
            epoch_progress_bar_metrics.update(cache_internal_epoch_pbar_metrics)
        # TODO: deprecate 1.0
        else:
            out = self.__run_legacy_training_epoch_end(
                num_optimizers,
                epoch_output,
                model,
                is_result_obj,
                epoch_callback_metrics
            )
            epoch_log_metrics, epoch_progress_bar_metrics, epoch_callback_metrics = out

        # --------------------------
        # track results
        # --------------------------
        # add the metrics to the loggers and callbacks
        if epoch_log_metrics and len(epoch_log_metrics) > 0:
            self.log_metrics(epoch_log_metrics, {})
            self.callback_metrics.update(epoch_log_metrics)

        # add metrics to callbacks
        self.callback_metrics.update(epoch_callback_metrics)

        # add metrics to progress_bar and callbacks
        if len(epoch_progress_bar_metrics) > 0:
            self.add_progress_bar_metrics(epoch_progress_bar_metrics)
            self.callback_metrics.update(epoch_progress_bar_metrics)

    def training_epoch_end(self, model, epoch_output, num_optimizers):
        if not is_overridden('training_epoch_end', model=model):
            return Result()

        # run training_epoch_end
        # refresh the result for custom logging at the epoch level
        model._current_fx_name = 'training_epoch_end'
        model._results = Result()

        epoch_output = self.__prepare_epoch_end_inputs(epoch_output)

        if num_optimizers == 1 or not self.trainer.train_loop.automatic_optimization:
            epoch_output = epoch_output[0]

        # lightningmodule hook
        epoch_output = model.training_epoch_end(epoch_output)

        model._current_fx_name = ''

        if epoch_output is not None:
            raise MisconfigurationException('training_epoch_end expects a return of None. '
                                            'HINT: remove the return statement in training_epoch_end')

        # user can ALSO log at the end of an epoch
        new_epoch_end_logs = model._results
        return new_epoch_end_logs

    def __run_legacy_training_epoch_end(
            self,
            num_optimizers,
            epoch_output,
            model,
            is_result_obj,
            epoch_callback_metrics
    ):

        epoch_log_metrics = {}
        epoch_progress_bar_metrics = {}

        # --------------------------
        # EPOCH END STEP IF DEFINED
        # --------------------------
        if is_overridden('training_epoch_end', model=model):
            if is_result_obj:
                # with result object gather across time and training steps so each opt idx has a single result obj
                epoch_output = self.__gather_result_across_time_and_optimizers(epoch_output)

            if num_optimizers == 1:
                epoch_output = epoch_output[0]

            # run training_epoch_end
            # a list with a result per optimizer index
            epoch_output = model.training_epoch_end(epoch_output)

            if isinstance(epoch_output, Result):
                epoch_log_metrics = epoch_output.epoch_log_metrics
                epoch_progress_bar_metrics = epoch_output.epoch_pbar_metrics
            else:
                _processed_outputs = self.trainer.process_dict_result(epoch_output)
                epoch_progress_bar_metrics = _processed_outputs[1]
                epoch_log_metrics = _processed_outputs[2]
                epoch_callback_metrics = _processed_outputs[3]

        # --------------------------
        # Structured Result (auto epoch end)
        # --------------------------
        elif is_result_obj:
            epoch_log_metrics, epoch_progress_bar_metrics = self.__auto_reduce_results_on_epoch_end(epoch_output)

        return epoch_log_metrics, epoch_progress_bar_metrics, epoch_callback_metrics

    def __auto_reduce_results_on_epoch_end(self, epoch_output):
        epoch_log_metrics = {}
        epoch_progress_bar_metrics = {}
        for opt_outputs in epoch_output:
            # reduce across time first
            time_reduced_outputs = []
            for train_step_idx in range(len(opt_outputs)):
                tbptt_outs = opt_outputs[train_step_idx]
                tbptt_outs = tbptt_outs[0].__class__.reduce_across_time(tbptt_outs)
                if len(tbptt_outs) > 1:
                    time_reduced_outputs.append(tbptt_outs)

            if len(time_reduced_outputs) == 0:
                continue

            # reduce across training steps
            opt_outputs = time_reduced_outputs[0].__class__.reduce_on_epoch_end(time_reduced_outputs)

            # with manual opt need 1+ metrics because meta is always there
            if opt_outputs.minimize is not None:
                opt_outputs.minimize = opt_outputs.minimize.mean()
            epoch_log_metrics.update(opt_outputs.epoch_log_metrics)
            epoch_progress_bar_metrics.update(opt_outputs.epoch_pbar_metrics)

        return epoch_log_metrics, epoch_progress_bar_metrics

    def __prepare_epoch_end_inputs(self, epoch_output):
        """
        Pulls out only the "extra" information for epoch end

        Return:
            a single list, each element per optimizer then batch then time
        """
        gathered_epoch_outputs = []
        for opt_outputs in epoch_output:
            # gather across time first
            time_gathered_outputs = []
            for train_step_idx in range(len(opt_outputs)):
                tbptt_outs = opt_outputs[train_step_idx]
                result = []
                for x in tbptt_outs:
                    out = x.extra
                    out['loss'] = x.minimize
                    result.append(out)

                # when time = 0, pass in the literal dict instead of array
                if len(result) == 1:
                    result = result[0]
                time_gathered_outputs.append(result)

            gathered_epoch_outputs.append(time_gathered_outputs)

        return gathered_epoch_outputs

    def __gather_result_across_time_and_optimizers(self, epoch_output):
        """
        Gather results into a single padded tensor per metric where each tensor is gathered across
        time and across time steps.

        Returns:
            a list where each element is a Result with the tensors gathered
        """
        gathered_epoch_outputs = []
        for opt_outputs in epoch_output:
            # gather across time first
            time_gathered_outputs = []
            for train_step_idx in range(len(opt_outputs)):
                tbptt_outs = opt_outputs[train_step_idx]
                tbptt_outs = tbptt_outs[0].__class__.gather(tbptt_outs)
                time_gathered_outputs.append(tbptt_outs)

            # gather across training steps
            # each metric has dimensions (training_steps, seq_len) (seq_len=1 when no tbptt is used)
            gathered_opt_output = time_gathered_outputs[0].__class__.padded_gather(time_gathered_outputs)
            gathered_epoch_outputs.append(gathered_opt_output)

        return gathered_epoch_outputs

    def log_train_step_metrics(self, batch_output):
        # when metrics should be logged
        should_log_metrics = (
            (self.trainer.global_step + 1) % self.trainer.log_every_n_steps == 0 or self.trainer.should_stop
        )
        if should_log_metrics or self.trainer.fast_dev_run:
            # logs user requested information to logger
            metrics = batch_output.batch_log_metrics
            grad_norm_dic = batch_output.grad_norm_dic
            if metrics is None:
                metrics = {}
            if grad_norm_dic is None:
                grad_norm_dic = {}
            if len(metrics) > 0 or len(grad_norm_dic) > 0:
                self.log_metrics(metrics, grad_norm_dic)
                self.callback_metrics.update(metrics)
