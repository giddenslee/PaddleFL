#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import print_function

import sys
import math
from functools import reduce

import collections
import six
import logging

import numpy as np
import paddle.fluid as fluid

from .details.ps_dispatcher import RoundRobin, PSDispatcher
from paddle.fluid import core, framework, unique_name
from paddle.fluid.framework import Program, default_main_program, \
    default_startup_program, Block, Parameter, grad_var_name
from .details import wait_server_ready, UnionFind, VarStruct, VarsDistributed
from .details import delete_ops, find_op_by_output_arg
from paddle.fluid.distribute_lookup_table import find_distributed_lookup_table
from paddle.fluid.transpiler.distribute_transpiler import DistributeTranspilerConfig, slice_variable

LOOKUP_TABLE_TYPE = "lookup_table"
LOOKUP_TABLE_GRAD_TYPE = "lookup_table_grad"
OP_ROLE_VAR_ATTR_NAME = core.op_proto_and_checker_maker.kOpRoleVarAttrName()
RPC_OP_ROLE_ATTR_NAME = op_role_attr_name = core.op_proto_and_checker_maker.kOpRoleAttrName(
)
OPT_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.Optimize
RPC_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.RPC
DIST_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.Dist
LR_SCHED_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.LRSched
PRINT_LOG = False


def log(*args):
    if PRINT_LOG:
        print(args)

def same_or_split_var(p_name, var_name):
    return p_name == var_name or p_name.startswith(var_name + ".block")


class FLDistributeTranspiler(object):
    """
    **FlDistributeTranspiler**

    Convert the fluid program to distributed data-parallelism programs.

    In pserver mode, the trainers' main program do forward, backward and optimizaiton.
    pserver's main_program will sum and scale.


    Examples:
        .. code-block:: python

            x = fluid.layers.data(name='x', shape=[13], dtype='float32')
            y = fluid.layers.data(name='y', shape=[1], dtype='float32')
            y_predict = fluid.layers.fc(input=x, size=1, act=None)

            cost = fluid.layers.square_error_cost(input=y_predict, label=y)
            avg_loss = fluid.layers.mean(cost)

            sgd_optimizer = fluid.optimizer.SGD(learning_rate=0.001)
            sgd_optimizer.minimize(avg_loss)

            # for pserver mode
            pserver_endpoints = "192.168.0.1:6174,192.168.0.2:6174"
            trainer_endpoints = "192.168.0.1:6174,192.168.0.2:6174"
            current_endpoint = "192.168.0.1:6174"
            trainer_id = 0
            trainers = 4
            role = "PSERVER"
            t = fluid.FlDistributeTranspiler()
            t.transpile(
                 trainer_id, pservers=pserver_endpoints, trainers=trainers)
            if role == "PSERVER":
                 pserver_program = t.get_pserver_program(current_endpoint)
                 pserver_startup_program = t.get_startup_program(current_endpoint,
                                                                pserver_program)
            elif role == "TRAINER":
                 trainer_program = t.get_trainer_program()

    """

    def __init__(self, config=None):
        if config is not None:
            self.config = config
        else:
            self.config = DistributeTranspilerConfig()

        if self.config.split_method is None:
            self.config.split_method = RoundRobin

        global PRINT_LOG
        if self.config.print_log:
            PRINT_LOG = True
        assert (self.config.min_block_size >= 8192)
        assert (self.config.split_method.__bases__[0] == PSDispatcher)

    def _get_all_remote_sparse_update_op(self, main_program):
        sparse_update_ops = []
        sparse_update_op_types = ["lookup_table", "nce", "hierarchical_sigmoid"]
        for op in main_program.global_block().ops:
            if op.type in sparse_update_op_types and op.attr(
                    'remote_prefetch') is True:
                sparse_update_ops.append(op)
        return sparse_update_ops

    def transpile(self,
                  trainer_id,
                  program=None,
                  pservers="127.0.0.1:6174",
                  trainers=1,
                  sync_mode=True,
                  startup_program=None,
                  current_endpoint="127.0.0.1:6174"):
        """
        Run the transpiler. Transpile the input program.

        Args:
            trainer_id (int): id for current trainer worker, if you have
                n workers, the id may range from 0 ~ n-1
            program (Program|None): program to transpile,
                default is fluid.default_main_program().
            startup_program (Program|None): startup_program to transpile,
                default is fluid.default_startup_program().
            pservers (str): comma separated ip:port string for the pserver
                list.
            trainers (int|str): in pserver mode this is the number of
                trainers.
            sync_mode (bool): Do sync training or not, default is True.
            startup_program (Program|None): startup_program to transpile,
                default is fluid.default_main_program().
            current_endpoint (str): In pserver mode
                this argument is not used.

        Examples:
            .. code-block:: python

                transpiler = fluid.DistributeTranspiler()
                t.transpile(
                    trainer_id=0,
                    pservers="127.0.0.1:7000,127.0.0.1:7001",
                    trainers=2,
                    sync_mode=False,
                    current_endpoint="127.0.0.1:7000")
        """
        if program is None:
            program = default_main_program()
        if startup_program is None:
            startup_program = default_startup_program()
        self.origin_program = program
        self.startup_program = startup_program
        self.origin_startup_program = self.startup_program.clone()

        self.trainer_num = trainers
        self.sync_mode = sync_mode
        self.trainer_id = trainer_id
        pserver_endpoints = pservers.split(",")
        self.pserver_endpoints = pserver_endpoints
        self.vars_overview = VarsDistributed()
        self.optimize_ops, self.params_grads = self._get_optimize_pass()

        ps_dispatcher = self.config.split_method(self.pserver_endpoints)
        self.table_name = find_distributed_lookup_table(self.origin_program)
        self.has_distributed_lookup_table = self.table_name != None
        self.param_name_to_grad_name = dict()
        self.grad_name_to_param_name = dict()
        for param_var, grad_var in self.params_grads:
            self.param_name_to_grad_name[param_var.name] = grad_var.name
            self.grad_name_to_param_name[grad_var.name] = param_var.name

        # get all sparse update ops
        self.sparse_update_ops = self._get_all_remote_sparse_update_op(
            self.origin_program)
        # use_sparse_update_param_name -> split_height_section
        self.sparse_param_to_height_sections = dict()

        # add distributed attrs to program
        self.origin_program._is_distributed = True
        self.origin_program._endpoints = self.pserver_endpoints
        self.origin_program._ps_endpoint = current_endpoint
        self.origin_program._is_chief = self.trainer_id == 0
        self.origin_program._distributed_lookup_table = self.table_name if self.table_name else None

        # split and create vars, then put splited vars in dicts for later use.
        # step 1: split and create vars, then put splited vars in dicts for later use.
        self._init_splited_vars()

        # step 2: insert send op to send gradient vars to parameter servers
        ps_dispatcher.reset()
        send_vars = []

        # in general cases, the number of pservers is times of 2, and this
        # will lead to uneven distribution among weights and bias:
        #       fc_w@GRAD_trainer_0, fc_w@GRAD_trainer_1 --> pserver1
        #       fc_b@GRAD_trainer_0, fc_b@GRAD_trainer_1 --> pserver2
        # shuffle the map will avoid the uneven distribution above

        self.opti_name_to_send_dummy_out = dict()
        self.recv_program = self.origin_program.clone()
        all_ops = []
        for op in self.recv_program.global_block().ops:
            all_ops.append(op)
        delete_ops(self.recv_program.global_block(), all_ops)

        self.split_num = len(program.global_block().ops)
        for opti_varname in self._opti_var_list:
            opti_var = program.global_block().var(opti_varname)
            eplist = ps_dispatcher.dispatch([opti_var])

            dummy_output = program.global_block().create_var(
                name=framework.generate_control_dev_var_name())
            self.opti_name_to_send_dummy_out[opti_varname] = dummy_output

            program.global_block().append_op(
                type="send",
                inputs={"X": [opti_var]},
                outputs={"Out": dummy_output},
                attrs={
                    "epmap": eplist,
                    RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE,
                    OP_ROLE_VAR_ATTR_NAME:
                    [self._opti_to_param[opti_varname], opti_varname],
                    "sync_mode": not self.sync_mode,
                })
            send_vars.append(opti_var)

        if self.sync_mode:
            send_barrier_out = program.global_block().create_var(
                name=framework.generate_control_dev_var_name())
            input_deps = list(self.opti_name_to_send_dummy_out.values())

            program.global_block().append_op(
                type="send_barrier",
                inputs={"X": list(input_deps)},
                outputs={"Out": send_barrier_out},
                attrs={
                    "endpoints": pserver_endpoints,
                    "sync_mode": self.sync_mode,
                    "trainer_id": self.trainer_id,
                    RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
                })

        # step 3: insert recv op to receive parameters from parameter server
        recv_vars = []
        for _, var in enumerate(send_vars):
            recv_vars.append(program.global_block().var(self._opti_to_param[
                var.name]))
        ps_dispatcher.reset()
        eplist = ps_dispatcher.dispatch(recv_vars)
        for i, ep in enumerate(eplist):
            self.param_grad_ep_mapping[ep]["params"].append(recv_vars[i])
            self.param_grad_ep_mapping[ep]["opti"].append(send_vars[i])

            distributed_var = self.vars_overview.get_distributed_var_by_slice(
                recv_vars[i].name)
            distributed_var.endpoint = ep

        # step4: Concat the parameters splits together after recv.
        all_recv_outputs = []
        for opti_varname in self._opti_var_list:
            opti_var = program.global_block().var(opti_varname)
            param_varname = self._opti_to_param[opti_varname]
            param_var = program.global_block().var(param_varname)
            eps = []
            table_names = []
            index = [v.name for v in recv_vars].index(param_varname)
            eps.append(eplist[index])
            table_names.append(var.name)
            if self.sync_mode:
                recv_dep_in = send_barrier_out
            # get recv op_role_var, if not splited, the grad should have .trainer suffix
            # if splited, grad should be the original grad var name. ParallelExecutor
            # will use op_role_var to get expected device place to run this op.

            all_recv_outputs.extend([param_var])
            self.recv_program.global_block().append_op(
                type="recv",
                inputs={"X": []},
                outputs={"Out": [param_var]},
                attrs={
                    "epmap": eps,
                    "trainer_id": self.trainer_id,
                    RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE,
                    OP_ROLE_VAR_ATTR_NAME: [param_varname, opti_varname],
                    "sync_mode": not self.sync_mode
                })

        if self.sync_mode:
            # form a WAW dependency
            self.recv_program.global_block()._insert_op(
                index=len(self._opti_var_list),
                type="fetch_barrier",
                inputs={},
                outputs={"Out": all_recv_outputs},
                attrs={
                    "endpoints": pserver_endpoints,
                    "trainer_id": self.trainer_id,
                    RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
                })

        self._get_trainer_startup_program(recv_vars=recv_vars, eplist=eplist)

        self._get_distributed_optimizer_vars()
        self.origin_program._parameters_on_pservers = self.vars_overview

    def get_trainer_program(self, wait_port=True):
        """
        Get transpiled trainer side program.

        Returns:
            Program: trainer side program.
        """
        # remove optimize ops and add a send op to main_program
        # FIXME(typhoonzero): Also ops like clip_gradient, lrn_decay?

        lr_ops = self._get_lr_ops()

        self.origin_program.__str__()

        self.send_program = self.origin_program.clone()
        compute_ops = self.send_program.global_block().ops[0:self.split_num]
        delete_ops(self.send_program.global_block(), compute_ops)
        send_ops = self.origin_program.global_block().ops[self.split_num:]
        delete_ops(self.origin_program.global_block(), send_ops)

        return self.recv_program, self.origin_program, self.send_program

    def _get_trainer_startup_program(self, recv_vars, eplist):
        """
        Get transpiled trainer side startup program.

        Args:
            recv_vars (list): Variable list to recv for current trainer_id
            eplist (list): A list of strings indicating

        Returns:
            Program: trainer side startup program.
        """
        startup_program = self.startup_program

        # FIXME(gongwb): delete not need ops.
        # note that: some parameter is not trainable and those ops can't be deleted.
        for opti_varname in self._opti_var_list:
            opti_var = self.origin_program.global_block().var(opti_varname)
            param_varname = self._opti_to_param[opti_varname]
            var = self.origin_program.global_block().var(param_varname)

            # Get the eplist of recv vars
            eps = []
            table_names = []
            index = [v.name for v in recv_vars].index(param_varname)
            eps.append(eplist[index])

        return startup_program

    def get_pserver_program(self, endpoint):
        """
        Get parameter server side program.

        Args:
            endpoint (str): current parameter server endpoint.

        Returns:
            Program: the program for current parameter server to run.
        """
        # TODO(panyx0718): Revisit this assumption. what if #blocks > #pservers.
        # NOTE: assume blocks of the same variable is not distributed
        # on the same pserver, only change param/grad varnames for
        # trainers to fetch.
        sys.stderr.write(
            "get_pserver_program() is deprecated, call get_pserver_programs() to get pserver main and startup in a single call.\n"
        )
        # step1
        pserver_program = Program()
        pserver_program.random_seed = self.origin_program.random_seed
        pserver_program._copy_dist_param_info_from(self.origin_program)

        # step2: Create vars to receive vars at parameter servers.
        recv_inputs = []
        for v in self.param_grad_ep_mapping[endpoint]["params"]:
            self._clone_var(pserver_program.global_block(), v)
        for v in self.param_grad_ep_mapping[endpoint]["opti"]:
            # create vars for each trainer in global scope, so
            # we don't need to create them when grad arrives.
            # change client side var name to origin name by
            # removing ".trainer_%d" suffix
            suff_idx = v.name.find(".opti.trainer_")
            if suff_idx >= 0:
                orig_var_name = v.name[:suff_idx]
            # NOTE: single_trainer_var must be created for multi-trainer
            # case to merge grads from multiple trainers
            single_trainer_var = pserver_program.global_block().var(
                    orig_var_name)

            if self.sync_mode and self.trainer_num > 1:
                for trainer_id in range(self.trainer_num):
                    var = pserver_program.global_block().create_var(
                        name="%s.opti.trainer_%d" % (orig_var_name, trainer_id),
                        persistable=False,
                        type=v.type,
                        dtype=v.dtype,
                        shape=v.shape)
                    recv_inputs.append(var)

        # step 3
        # Create a union-find data structure from optimize ops,
        # If two ops are connected, we could add these two ops
        # into one set.
        ufind = self._create_ufind(self.optimize_ops)
        # step 3.2
        # Iterate through the ops and append optimize op which
        # located on current pserver
        opt_op_on_pserver = []
        for _, op in enumerate(self.optimize_ops):
            if self._is_optimizer_op(op) and self._is_opt_op_on_pserver(
                    endpoint, op):
                opt_op_on_pserver.append(op)

        # step 3.4
        # Iterate through the ops, and if an op and the optimize ops
        # which located on current pserver are in one set, then
        # append it into the sub program.

        global_ops = []

        # sparse grad name to param name
        sparse_grad_to_param = []

        # append lr decay ops to the child block if exists
        lr_ops = self._get_lr_ops()
        # record optimize blocks and we can run them on pserver parallel
        opti_blocks = []

        # append op to the current block
        grad_to_block_id = []
        pre_block_idx = pserver_program.num_blocks - 1
        for idx, opt_op in enumerate(self._opti_var_list):
            per_opt_block = pserver_program._create_block(pre_block_idx)
            opti_blocks.append(per_opt_block)
            optimize_target_param_name = self._opti_to_param[opt_op]
            pserver_block = per_opt_block.program.global_block()
            # append grad merging ops before clip and weight decay
            # e.g. merge grad -> L2Decay op -> clip op -> optimize
            merged_var = pserver_block.vars[optimize_target_param_name]
            if self.sync_mode and self.trainer_num > 1:
                vars2merge = []
                for i in range(self.trainer_num):
                    per_trainer_name = "%s.opti.trainer_%d" % \
                                       (optimize_target_param_name, i)
                    vars2merge.append(pserver_block.vars[per_trainer_name])
                per_opt_block.append_op(
                    type="sum",
                    inputs={"X": vars2merge},
                    outputs={"Out": merged_var},
                    attrs={"use_mkldnn": False})
                per_opt_block.append_op(
                    type="scale",
                    inputs={"X": merged_var},
                    outputs={"Out": merged_var},
                    attrs={"scale": 1.0 / float(self.trainer_num)})

        # In some case, some parameter server will have no parameter to optimize
        # So we give an empty optimize block to parameter server.
        attrs = {
            "optimize_blocks": opti_blocks,
            "endpoint": endpoint,
            "Fanin": self.trainer_num,
            "sync_mode": self.sync_mode,
        }

        # step5 append the listen_and_serv op
        pserver_program.global_block().append_op(
            type="fl_listen_and_serv",
            inputs={'X': recv_inputs},
            outputs={},
            attrs=attrs)

        pserver_program._sync_with_cpp()
        # save pserver program to generate pserver side startup relatively.
        self.pserver_program = pserver_program
        return pserver_program

    def get_startup_program(self,
                            endpoint,
                            pserver_program=None,
                            startup_program=None):
        """
        **Deprecated**

        Get startup program for current parameter server.
        Modify operator input variables if there are variables that
        were split to several blocks.

        Args:
            endpoint (str): current pserver endpoint.
            pserver_program (Program): deprecated, call get_pserver_program first.
            startup_program (Program): deprecated, should pass startup_program
                when initalizing

        Returns:
            Program: parameter server side startup program.
        """
        s_prog = Program()
        orig_s_prog = self.startup_program
        s_prog.random_seed = orig_s_prog.random_seed
        params = self.param_grad_ep_mapping[endpoint]["params"]

        def _get_splited_name_and_shape(varname):
            for idx, splited_param in enumerate(params):
                pname = splited_param.name
                if same_or_split_var(pname, varname) and varname != pname:
                    return pname, splited_param.shape
            return "", []

        # 1. create vars in pserver program to startup program
        pserver_vars = pserver_program.global_block().vars
        created_var_map = collections.OrderedDict()
        for _, var in six.iteritems(pserver_vars):
            tmpvar = s_prog.global_block()._clone_variable(var)
            created_var_map[var.name] = tmpvar

        # 2. rename op outputs
        for op in orig_s_prog.global_block().ops:
            new_outputs = collections.OrderedDict()
            # do not append startup op if var is not on this pserver
            op_on_pserver = False
            # TODO(gongwb): remove this line.
            if op.type not in ["recv", "fetch_barrier", "concat"]:
                for key in op.output_names:
                    newname, _ = _get_splited_name_and_shape(op.output(key)[0])
                    if newname:
                        op_on_pserver = True
                        new_outputs[key] = created_var_map[newname]
                    elif op.output(key)[0] in pserver_vars:
                        op_on_pserver = True
                        new_outputs[key] = pserver_vars[op.output(key)[0]]

            if op_on_pserver:
                # most startup program ops have no inputs
                new_inputs = self._get_input_map_from_op(pserver_vars, op)

                if op.type in [
                        "gaussian_random", "fill_constant", "uniform_random",
                        "truncated_gaussian_random"
                ]:
                    op._set_attr("shape", list(new_outputs["Out"].shape))
                s_prog.global_block().append_op(
                    type=op.type,
                    inputs=new_inputs,
                    outputs=new_outputs,
                    attrs=op.all_attrs())

        return s_prog

    # ====================== private transpiler functions =====================
    def _get_slice_var_info(self, slice_var):
        block_suffix = "block"
        block_idx = 0
        offset = 0
        is_slice = False

        orig_var_name, block_name, _ = self._get_varname_parts(slice_var.name)

        if not block_name:
            return is_slice, block_idx, offset

    def _get_distributed_optimizer_vars(self):
        def _get_distributed_optimizer_var(endpoint):
            opt_op_on_pserver = []
            for _, op in enumerate(self.optimize_ops):
                if self._is_optimizer_op(op) and self._is_opt_op_on_pserver(
                        endpoint, op):
                    opt_op_on_pserver.append(op)

            for opt_op in opt_op_on_pserver:
                dist_var = None
                for key in opt_op.input_names:
                    if key == "Param":
                        param_name = opt_op.input(key)[0]
                        dist_var = self.vars_overview.get_distributed_var_by_origin_and_ep(
                            param_name, endpoint)
                        break
                for key in opt_op.input_names:
                    if key in ["Param", "Grad", "LearningRate"]:
                        continue

        for ep in self.pserver_endpoints:
            _get_distributed_optimizer_var(ep)

    def _update_dist_lookup_table_vars(self, param_list, grad_list,
                                       params_grads):
        # TODO(wuyi): put find a way to put dist lookup table stuff all together.
        # update self.table_param_grad and self.trainer_side_table_grad_list
        program = self.origin_program
        return param_list, grad_list

    def _init_splited_vars(self):
        # update these mappings for further transpile:
        # 1. param_var_mapping: param var name -> [splited params vars]
        # 2. grad_var_mapping: grad var name -> [splited grads vars]
        # 3. grad_param_mapping: grad.blockx -> param.blockx
        # 4. param_grad_ep_mapping: ep -> {"params": [], "grads": []}

        param_list = []
        grad_list = []
        param_grad_set = set()
        for p, g in self.params_grads:
            # skip parameter marked not trainable
            if type(p) == Parameter and p.trainable == False:
                continue
            if p.name not in param_grad_set:
                param_list.append(p)
                param_grad_set.add(p.name)
            if g.name not in param_grad_set:
                grad_list.append(g)
                param_grad_set.add(g.name)

        # To do : consider lookup table later
        param_list, grad_list = self._update_dist_lookup_table_vars(
            param_list, grad_list, self.params_grads)

        if self.config.slice_var_up:
            # when we slice var up into blocks, we will slice the var according to
            # pserver services' count. A pserver may have two or more listening ports.
            grad_blocks = slice_variable(grad_list,
                                         len(self.pserver_endpoints),
                                         self.config.min_block_size)
            param_blocks = slice_variable(param_list,
                                          len(self.pserver_endpoints),
                                          self.config.min_block_size)
        assert (len(grad_blocks) == len(param_blocks))

        # origin_param_name -> [splited_param_vars]
        self.param_var_mapping = self._create_vars_from_blocklist(
            self.origin_program, param_blocks)

        for orig_name, splited_vars in self.param_var_mapping.items():
            orig_var = self.origin_program.global_block().var(orig_name)
            for splited_var in splited_vars:
                is_slice, block_id, offset = self._get_slice_var_info(
                    splited_var)

                self.vars_overview.add_distributed_var(
                    origin_var=orig_var,
                    slice_var=splited_var,
                    block_id=block_id,
                    offset=offset,
                    is_slice=is_slice,
                    vtype="Param")

        # origin_grad_name -> [splited_grad_vars]
        self.grad_var_mapping = self._create_vars_from_blocklist(
            self.origin_program, grad_blocks)
        #add_trainer_suffix=self.trainer_num > 1)
        # dict(grad_splited_var -> param_splited_var)
        self.grad_param_mapping = collections.OrderedDict()
        for g, p in zip(grad_blocks, param_blocks):
            g_name, g_bid, _ = g.split(":")
            p_name, p_bid, _ = p.split(":")
            self.grad_param_mapping[self.grad_var_mapping[g_name][int(g_bid)]] = \
                self.param_var_mapping[p_name][int(p_bid)]

        # create mapping of endpoint -> split var to create pserver side program
        self.param_grad_ep_mapping = collections.OrderedDict()
        [
            self.param_grad_ep_mapping.update({
                ep: {
                    "params": [],
                    "opti": []
                }
            }) for ep in self.pserver_endpoints
        ]

        opti_list = []
        opti_to_param = dict()
        param_to_opti = dict()
        for op in self.optimize_ops:
            if op.type == "sgd":
                origin_name = op.output("ParamOut")
                var = self.origin_program.global_block().var(origin_name[0])
                new_var_name = "%s.opti.trainer_%d" % (origin_name[0],
                                                       self.trainer_id)
                self.origin_program.global_block().create_var(
                    name=new_var_name,
                    persistable=True,
                    shape=var.shape,
                    dtype=var.dtype,
                    type=var.type,
                    lod_level=var.lod_level)
                new_var = self.origin_program.global_block().var(new_var_name)
                opti_list.append(new_var.name)
                opti_to_param[new_var.name] = var.name
                param_to_opti[var.name] = new_var.name
                self.origin_program.global_block().append_op(
                    type="scale",
                    inputs={"X": var},
                    outputs={"Out": new_var},
                    attrs={"scale": 1.0})
        self._param_to_opti = param_to_opti
        self._opti_to_param = opti_to_param
        self._opti_var_list = opti_list

    def _create_vars_from_blocklist(self,
                                    program,
                                    block_list,
                                    add_trainer_suffix=False):
        """
        Create vars for each split.
        NOTE: only grads need to be named for different trainers, use
              add_trainer_suffix to rename the grad vars.
        Args:
            program (ProgramDesc): ProgramDesc which gradients blong.
            block_list (list[(varname, block_id, block_size)]): List of gradient blocks.
            add_trainer_suffix (Bool): Add trainer suffix to new variable's name if set True.
        Returns:
            var_mapping (collections.OrderedDict(varname->[new_varname_variable])):A dict mapping
                from original var name to each var split.
        """

        # varname->[(block_id, current_block_size)]
        block_map = collections.OrderedDict()

        var_mapping = collections.OrderedDict()
        for block_str in block_list:
            varname, offset, size = block_str.split(":")
            if varname not in block_map:
                block_map[varname] = []
            block_map[varname].append((int(offset), int(size)))

        for varname, splited in six.iteritems(block_map):
            orig_var = program.global_block().var(varname)
            if len(splited) == 1:
                var_mapping[varname] = \
                        [program.global_block().var(orig_var.name)]
                continue
        return var_mapping

    def _clone_var(self, block, var, persistable=True):
        return block.create_var(
            name=var.name,
            shape=var.shape,
            dtype=var.dtype,
            type=var.type,
            lod_level=var.lod_level,
            persistable=persistable)

    def _get_varname_parts(self, varname):
        # returns origin, blockid, trainerid
        orig_var_name = ""
        trainer_part = ""
        block_part = ""
        trainer_idx = varname.find(".trainer_")
        if trainer_idx >= 0:
            trainer_part = varname[trainer_idx + 1:]
        else:
            trainer_idx = len(varname)
        block_index = varname.find(".block")
        if block_index >= 0:
            block_part = varname[block_index + 1:trainer_idx]
        else:
            block_index = len(varname)
        orig_var_name = varname[0:min(block_index, trainer_idx)]
        return orig_var_name, block_part, trainer_part

    def _is_op_connected(self, op1, op2):
        # If one op's input is another op's output or
        # one op's output is another op's input, we say
        # the two operator is connected.
        if set(op1.desc.output_arg_names()) & set(op2.desc.input_arg_names()) or \
                set(op1.desc.input_arg_names()) & set(op2.desc.output_arg_names()):
            return True
        return False

    def _create_ufind(self, optimize_ops):
        # Create a unit find data struct by optimize ops
        ufind = UnionFind(optimize_ops)
        for i in range(len(optimize_ops)):
            for j in range(i, len(optimize_ops)):
                op1 = optimize_ops[i]
                op2 = optimize_ops[j]
                if self._is_op_connected(op1, op2):
                    ufind.union(op1, op2)
        return ufind

    def _is_optimizer_op(self, op):
        if "Param" in op.input_names and \
                "LearningRate" in op.input_names:
            return True
        return False

    def _is_opt_op_on_pserver(self, endpoint, op):
        param_names = [
            p.name for p in self.param_grad_ep_mapping[endpoint]["params"]
        ]
        if op.input("Param")[0] in param_names:
            return True

    def _get_input_map_from_op(self, varmap, op):
        """Returns a dict from op input name to the vars in varmap."""
        iomap = collections.OrderedDict()
        return iomap


    def _get_lr_ops(self):
        lr_ops = []
        block = self.origin_program.global_block()
        for op in block.ops:
            role_id = int(op.attr(RPC_OP_ROLE_ATTR_NAME))
        return lr_ops

    def _is_opt_role_op(self, op):
        # NOTE: depend on oprole to find out whether this op is for
        # optimize
        op_maker = core.op_proto_and_checker_maker
        optimize_role = core.op_proto_and_checker_maker.OpRole.Optimize
        if op_maker.kOpRoleAttrName() in op.attr_names and \
                int(op.all_attrs()[op_maker.kOpRoleAttrName()]) == int(optimize_role):
            return True
        return False

    def _get_optimize_pass(self):
        """
        Get optimizer operators, parameters and gradients from origin_program
        Returns:
            opt_ops (list): optimize operators.
            params_grads (dict): parameter->gradient.
        """
        block = self.origin_program.global_block()
        opt_ops = []
        params_grads = []
        # tmp set to dedup
        optimize_params = set()
        origin_var_dict = self.origin_program.global_block().vars
        for op in block.ops:
            if self._is_opt_role_op(op):
                opt_ops.append(op)
                if op.attr(OP_ROLE_VAR_ATTR_NAME):
                    param_name = op.attr(OP_ROLE_VAR_ATTR_NAME)[0]
                    grad_name = op.attr(OP_ROLE_VAR_ATTR_NAME)[1]
                    if not param_name in optimize_params:
                        optimize_params.add(param_name)
                        log("adding param_grad pair: ", param_name, grad_name)
                        params_grads.append([
                            origin_var_dict[param_name],
                            origin_var_dict[grad_name]
                        ])
        return opt_ops, params_grads
