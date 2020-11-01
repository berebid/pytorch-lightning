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
from typing import List

import torch

from pytorch_lightning.accelerators import DDPAccelerator
from pytorch_lightning.overrides.fairscale import LightningOSS, LightningShardedDataParallel
from pytorch_lightning.utilities import AMPType


class DDPShardedAccelerator(DDPAccelerator):

    def __init__(self, trainer, cluster_environment=None):
        super().__init__(trainer, cluster_environment)
        self.nickname = 'ddp_sharded'

    def setup_optimizers(self, model):
        if self.trainer.testing is True:
            return

        optimizers, lr_schedulers, optimizer_frequencies = self.trainer.init_optimizers(model)
        self.trainer.optimizers = self.re_init_with_fairscale_zero(optimizers)
        self.trainer.lr_schedulers = lr_schedulers
        self.trainer.optimizer_frequencies = optimizer_frequencies

    def re_init_with_fairscale_zero(self, optimizers):
        """
        Re-initialise optimizers to use OSS wrapper. We need to re-initialise due to
        the parameters being sharded across distributed processes, each optimizing a partition.
        Args:
            optimizers: Input optimizers for trainer.
        Returns: Optimizers re-initialised using FairScale OSS (ZERO optimizer).

        """
        fairscale_zero_optimizers = []
        for optimizer in optimizers:
            optim_class = type(optimizer)
            zero_optimizer = LightningOSS(
                params=optimizer.param_groups,
                optim=optim_class,
                **optimizer.defaults
            )
            fairscale_zero_optimizers.append(zero_optimizer)
            del optimizer
        return fairscale_zero_optimizers

    def sync_optim_state(self):
        for optimizer in self.trainer.optimizers:
            optimizer.consolidate_state_dict()

    def configure_ddp(
            self, model: "LightningModule", device_ids: List[int]
    ):
        model = LightningShardedDataParallel(model, sharded_optimizer=self.trainer.optimizers)
        return model

    def training_step(self, args):
        return self._trainer_step(args)

    def validation_step(self, args):
        return self._trainer_step(args)

    def test_step(self, args):
        return self._trainer_step(args)

    def _trainer_step(self, args):
        if self.trainer.on_gpu:
            batch = args[0]
            batch = self.batch_to_device(batch, self.trainer.root_gpu)
            args[0] = batch

        if self.trainer.amp_backend == AMPType.NATIVE:
            with torch.cuda.amp.autocast():
                output = self.trainer.model(*args)
        else:
            output = self.trainer.model(*args)
        return output
