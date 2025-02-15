import numpy as np

from .data import Data
from .. import backend as bkd
from .. import config
from ..utils import run_if_all_none


class PDEOperator(Data):
    """PDE solution operator.

    Args:
        pde: Instance of ``dde.data.PDE`` or ``dde.data.TimePDE``.
        function_space: Instance of ``dde.data.FunctionSpace``.
        evaluation_points: A NumPy array of shape (n_points, dim). Discretize the input
            function sampled from `function_space` using pointwise evaluations at a set
            of points as the input of the branch net.
        num_function (int): The number of functions for training.
        function_variables: ``None`` or a list of integers. The functions in the
            `function_space` may not have the same domain as the PDE. For example, the
            PDE is defined on a spatio-temporal domain (`x`, `t`), but the function is
            IC, which is only a function of `x`. In this case, we need to specify the
            variables of the function by `function_variables=[0]`, where `0` indicates
            the first variable `x`. If ``None``, then we assume the domains of the
            function and the PDE are the same.
        num_test: The number of functions for testing PDE loss. The testing functions
            for BCs/ICs are the same functions used for training. If ``None``, then the
            training functions will be used for testing.

    Attributes:
        train_x_bc: A triple of three Numpy arrays (v, x, vx) fed into PIDeepONet for
            training BCs/ICs.
        num_bcs (list): `num_bcs[i]` is the number of points for `bcs[i]`.
        train_x: A triple of three Numpy arrays (v, x, vx) fed into PIDeepONet for
            training. v is the function input to the branch net; x is the point input to
            the trunk net; vx is the value of v evaluated at x, i.e., v(x). `train_x` is
            ordered from BCs/ICs (`train_x_bc`) to PDEs.
        test_x: A triple of three Numpy arrays (v, x, vx) fed into PIDeepONet for
            testing.
    """

    def __init__(
        self,
        pde,
        function_space,
        evaluation_points,
        num_function,
        function_variables=None,
        num_test=None,
    ):
        self.pde = pde
        self.func_space = function_space
        self.eval_pts = evaluation_points
        self.num_func = num_function
        self.func_vars = (
            function_variables
            if function_variables is not None
            else list(range(pde.geom.dim))
        )
        self.num_test = num_test

        self.num_bcs = [n * self.num_func for n in self.pde.num_bcs]
        self.train_x_bc = None
        self.train_x = None
        self.train_y = None
        self.test_x = None
        self.test_y = None

        self.train_next_batch()
        self.test()

    def losses(self, targets, outputs, loss, model):
        f = []
        if self.pde.pde is not None:
            f = self.pde.pde(model.net.inputs[1], outputs, model.net.inputs[2])
            if not isinstance(f, (list, tuple)):
                f = [f]

        bcs_start = np.cumsum([0] + self.num_bcs)
        error_f = [fi[bcs_start[-1] :] for fi in f]
        losses = [loss(bkd.zeros_like(error), error) for error in error_f]
        for i, bc in enumerate(self.pde.bcs):
            beg, end = bcs_start[i], bcs_start[i + 1]
            # The same BC points are used for training and testing.
            error = bc.error(
                self.train_x[1],
                model.net.inputs[1],
                outputs,
                beg,
                end,
                aux_var=self.train_x[2],
            )
            losses.append(loss(bkd.zeros_like(error), error))
        return losses

    @run_if_all_none("train_x", "train_y")
    def train_next_batch(self, batch_size=None):
        func_feats = self.func_space.random(self.num_func)
        func_vals = self.func_space.eval_batch(func_feats, self.eval_pts)
        v, x, vx = self.bc_inputs(func_feats, func_vals)
        if self.pde.pde is not None:
            v_pde, x_pde, vx_pde = self.gen_inputs(
                func_feats, func_vals, self.pde.train_x_all
            )
            v = np.vstack((v, v_pde))
            x = np.vstack((x, x_pde))
            vx = np.vstack((vx, vx_pde))
        self.train_x = (v, x, vx)
        self.train_y = None
        return self.train_x, self.train_y

    @run_if_all_none("test_x", "test_y")
    def test(self):
        if self.num_test is None:
            self.test_x = self.train_x
        else:
            func_feats = self.func_space.random(self.num_test)
            func_vals = self.func_space.eval_batch(func_feats, self.eval_pts)
            # TODO: Use different BC data from self.train_x
            v, x, vx = self.train_x_bc
            if self.pde.pde is not None:
                v_pde, x_pde, vx_pde = self.gen_inputs(
                    func_feats, func_vals, self.pde.test_x[sum(self.pde.num_bcs) :]
                )
                v = np.vstack((v, v_pde))
                x = np.vstack((x, x_pde))
                vx = np.vstack((vx, vx_pde))
            self.test_x = (v, x, vx)
        self.test_y = None
        return self.test_x, self.test_y

    def gen_inputs(self, func_feats, func_vals, points):
        # Format:
        # v1, x_1
        # ...
        # v1, x_N1
        # v2, x_1
        # ...
        # v2, x_N1
        v = np.repeat(func_vals, len(points), axis=0)
        x = np.tile(points, (len(func_feats), 1))
        vx = self.func_space.eval_batch(func_feats, points[:, self.func_vars]).reshape(
            -1, 1
        )
        return v, x, vx

    def bc_inputs(self, func_feats, func_vals):
        if not self.pde.bcs:
            self.train_x_bc = (
                np.empty((0, len(self.eval_pts)), dtype=config.real(np)),
                np.empty((0, self.pde.geom.dim), dtype=config.real(np)),
                np.empty((0, 1), dtype=config.real(np)),
            )
            return self.train_x_bc
        v, x, vx = [], [], []
        bcs_start = np.cumsum([0] + self.pde.num_bcs)
        for i, _ in enumerate(self.pde.num_bcs):
            beg, end = bcs_start[i], bcs_start[i + 1]
            vi, xi, vxi = self.gen_inputs(
                func_feats, func_vals, self.pde.train_x_bc[beg:end]
            )
            v.append(vi)
            x.append(xi)
            vx.append(vxi)
        self.train_x_bc = (np.vstack(v), np.vstack(x), np.vstack(vx))
        return self.train_x_bc
