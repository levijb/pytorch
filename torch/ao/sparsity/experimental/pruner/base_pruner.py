
import copy
import warnings
import abc

import torch
from torch import nn
from torch.nn.utils import parametrize

from torch.nn.modules.container import ModuleDict, ModuleList

from .parametrization import PruningParametrization, ZeroesParametrization, ActivationReconstruction, BiasHook

from torch.ao.sparsity import BaseSparsifier, module_to_fqn, fqn_to_module
from torch.ao.sparsity.sparsifier.utils import get_arg_info_from_tensor_fqn

__all__ = ["BasePruner"]

SUPPORTED_MODULES = {  # added to config if None given
    nn.Linear,
    nn.Conv2d,
    nn.BatchNorm2d,  # will need manual update to match conv2d
}

NEEDS_ZEROS = {  # these layers should have pruned indices zero-ed, not removed
    nn.BatchNorm2d
}

class BasePruner(BaseSparsifier):
    r"""Base class for all pruners.

    Abstract methods that need to be implemented:

    - update_mask: Function to compute a new mask for all keys in the
        `groups` attribute.

    Args:
        - defaults [dict]: default configurations will be attached to the
            configuration. Only the keys that don't exist in the `config` will
            be updated.
        - also_prune_bias [bool]: whether to prune bias in addition to weights (to prune full output channel)
            or not; default=True.

    """
    def __init__(self, defaults, also_prune_bias=True):
        super().__init__(defaults)
        self.prune_bias = also_prune_bias

    def _get_modules_and_tensor_names(self, config, use_path):
        modules = []
        tensor_names = []
        if use_path:
            if type(config['module']) is tuple:  # (Conv2d, BN)
                for module_fqn, tensor_name in zip(config['module_fqn'], config['tensor_name']):
                    module = fqn_to_module(self.model, module_fqn)
                    modules.append(module)
                    tensor_names.append(tensor_name)
            else:
                module = fqn_to_module(self.model, config['module_fqn'])
                modules.append(module)
                tensor_name = config['tensor_name']
                tensor_names.append(tensor_name)

        else:
            if type(config['module']) is tuple:
                for module, tensor_name in zip(config['module'], config['tensor_name']):
                    modules.append(module)
                    tensor_names.append(tensor_name)
            else:
                module = config['module']
                modules.append(module)
                tensor_name = config['tensor_name']
                tensor_names.append(tensor_name)
        return modules, tensor_names

    def _prepare(self, use_path=False, *args, **kwargs):
        r"""Adds mask parametrization to the layer weight
        """
        self.activation_handles = []  # store removable hook handles
        self.bias_handles = []

        for config in self.groups:
            modules, tensor_names = self._get_modules_and_tensor_names(config, use_path)

            for module, tensor_name in zip(modules, tensor_names):
                if not isinstance(module, tuple(NEEDS_ZEROS)):
                    # add pruning parametrization and forward hooks
                    if getattr(module, 'mask', None) is None:
                        module.register_buffer('mask', torch.tensor(getattr(module, tensor_name).shape[0]))
                    param = config.get('parametrization', PruningParametrization)
                    parametrize.register_parametrization(module, tensor_name, param(module.mask), unsafe=True)

                    assert isinstance(module.parametrizations, ModuleDict)  # make mypy happy
                    assert isinstance(module.parametrizations.weight, ModuleList)
                    if isinstance(module, tuple(SUPPORTED_MODULES)):
                        self.activation_handles.append(module.register_forward_hook(
                            ActivationReconstruction(getattr(module.parametrizations, tensor_name)[0])
                        ))
                    else:
                        raise NotImplementedError("This module type is not supported yet.")

                else:  # needs zeros
                    if getattr(module, 'mask', None) is None:
                        module.register_buffer('mask', torch.tensor(getattr(module, tensor_name).shape[0]))
                    param = config.get('parametrization', ZeroesParametrization)
                    parametrize.register_parametrization(module, tensor_name, param(module.mask), unsafe=True)

                if module.bias is not None:
                    module.register_parameter('_bias', nn.Parameter(module.bias.detach()))
                    module.bias = None
                self.bias_handles.append(module.register_forward_hook(BiasHook(module.parametrizations.weight[0], self.prune_bias)))

            if len(modules) == 2:  # (Conv2d, BN)
                # should have the same set of pruned outputs
                modules[1].parametrizations.weight[0].pruned_outputs = modules[0].parametrizations.weight[0].pruned_outputs

    def make_config_from_model(self, model, SUPPORTED_MODULES=SUPPORTED_MODULES, NEEDS_ZEROS=NEEDS_ZEROS):
        self.config = []
        stack = [model]
        while stack:
            module = stack.pop()
            for name, child in module.named_children():
                if type(child) in SUPPORTED_MODULES:
                    self.config.append({'tensor_fqn': module_to_fqn(model, child) + '.weight'})
                else:
                    if NEEDS_ZEROS is not None and type(child) in NEEDS_ZEROS and hasattr(self, "prune_bias") and self.prune_bias:
                        # only useful for Pruner
                        warnings.warn(f"Models with {type(child)} layers have config provided by user.")
                    stack.append(child)

    def prepare(self, model, config):
        r"""Prepares a model, by adding the parametrizations and forward post-hooks.
        Note::
            The model is modified inplace. If you need to preserve the original
            model, use copy.deepcopy.

        Args:
        - model [nn.Module]: model to configure. The model itself is not saved
            but used for the state_dict saving / loading.
        - config [list]: configuration elements could either be instances of
            tuples of dict maps or dict maps. The dicts must have a key 'tensor_fqn' with the
            value being the fqn of the tensor to be pruned.
        """
        self.model = model  # TODO: Need to figure out how to load without this.
        self.config = config

        # If no config -- try getting all the supported layers
        if self.config is None:
            # Add all models to the config
            self.make_config_from_model(self.model)

        for module_config in self.config:
            if type(module_config) is tuple:
                first_layer, next_layer = module_config
                assert isinstance(first_layer, nn.Conv2d) and isinstance(next_layer, nn.BatchNorm2d)
                module_config = {'module': module_config}
                local_args = copy.deepcopy(self.defaults)
                local_args.update(module_config)
                module_fqn_list = []
                tensor_fqn_list = []
                tensor_name_list = []
                for module in local_args['module']:
                    module_fqn = module_to_fqn(model, module)
                    if module_fqn is None:
                        module_fqn = ''
                    if module_fqn and module_fqn[0] == '.':
                        module_fqn = module_fqn[1:]
                    module_fqn_list.append(module_fqn)
                    tensor_fqn_list.append(module_fqn + '.weight')
                    tensor_name_list.append('weight')

                local_args['module_fqn'] = module_fqn_list
                local_args['tensor_fqn'] = tensor_fqn_list
                local_args['tensor_name'] = tensor_name_list
            else:
                if isinstance(module_config, nn.Module):
                    module_config = {'module': module_config}

                local_args = copy.deepcopy(self.defaults)
                local_args.update(module_config)

                # now that we're working with a dict, does it have the new format?
                if local_args.get('tensor_fqn', None) is not None:
                    tensor_fqn = local_args.get('tensor_fqn')
                    info_from_tensor_fqn = get_arg_info_from_tensor_fqn(model, tensor_fqn)

                    for key in info_from_tensor_fqn.keys():
                        if key in local_args:
                            # info_from_tensor_fqn will chop leading '.' from tensor_fqn so ignore that
                            assert key == 'tensor_fqn' or info_from_tensor_fqn[key] == local_args[key], (
                                "Given both `{}` and `tensor_fqn`, it is expected them to "
                                "agree!".format(key)
                            )
                    local_args.update(info_from_tensor_fqn)
                else:
                    module = local_args['module']
                    module_fqn = module_to_fqn(model, module)
                    if module_fqn and module_fqn[0] == '.':
                        module_fqn = module_fqn[1:]
                    local_args['module_fqn'] = module_fqn
                    local_args['tensor_name'] = "weight"
                    local_args['tensor_fqn'] = module_fqn + ".weight"
            self.groups.append(local_args)

        self._prepare()

    def squash_mask(self, use_path=False, *args, **kwargs):
        for config in self.groups:
            modules, tensor_names = self._get_modules_and_tensor_names(config, use_path)

            for module, tensor_name in zip(modules, tensor_names):
                parametrize.remove_parametrizations(module, tensor_name,
                                                    leave_parametrized=True)
                if getattr(module._parameters, 'mask', None):
                    del module._parameters['mask']
                elif getattr(module._buffers, 'mask', None):
                    del module._buffers['mask']
                delattr(module, 'mask')

    def get_module_pruned_outputs(self, module, tensor_name='weight'):
        r"""Returns the set of pruned indices of module"""
        assert parametrize.is_parametrized(module)  # can only get pruned indices of pruned module
        return getattr(module.parametrizations, tensor_name)[0].pruned_outputs  # assume only one parametrization attached

    def step(self, use_path=False):
        if not self.enable_mask_update:
            return
        with torch.no_grad():
            for config in self.groups:
                modules, tensor_names = self._get_modules_and_tensor_names(config, use_path)

                untupled_args: dict = {}
                untupled_args.update()
                # only need to update the first module in modules if len(modules) > 1
                # since they should share the same set of pruned outputs
                untupled_args['module'] = modules[0]
                untupled_args['tensor_name'] = tensor_names[0]
                self.update_mask(**config)

    @abc.abstractmethod
    def update_mask(self, module, tensor_name, **kwargs):
        pass
