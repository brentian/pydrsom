"""
the original dimension-reduced second-order method (DRSOM) in radius free mode
@author: Chuwen Zhang<chuwzhang@gmail.com>, Yinyu Ye<yinyu-ye@stanford.edu>
@note:
  This is a vanilla implementation of (Mini-batch, Radius-Free) DRSOM.
  - the options to run DRSOM can be controlled by the environment variables, see `drsom_utils.py`
  - we treat the parameters as a set of tensors (no flatten() anymore)
"""
from functools import reduce
from pprint import pprint
from typing import Optional
import torch
import numpy as np
import pandas as pd
from torch.profiler import profile, record_function, ProfilerActivity
from torch.nn.utils import parameters_to_vector

from .drsom_utils import *


class DRSOMB(torch.optim.Optimizer):
  
  def __init__(
      self,
      params,
      max_iter=15,
      option_tr="a",
      gamma=1e-6,
      beta1=5e1,
      beta2=3e1,
      hessian_window=1,
      thetas=(0.99, 0.999),
      decay_window=8000,
      decay_step=5e2,
      decay_radius_step=2e-1,
      eps=1e-8,
  ):
    """
    The DRSOM:
      Implementation of (Mini-batch) DRSOM (Dimension-Reduced Second-Order Method) in F (Radius-Free) style
    Args:
      model: model
      max_iter: # of iteration for trust-region adjustment
      option_tr: option of trust-region, I or G?
               - if 'a'; G = eye(2)
               - if 'p'; G = [-g d]'[-g d]
      gamma: lower bound for gamma
      beta1: gamma + multiplier
      beta2: gamma - multiplier
      hessian_window: window size to keep last k hessian information
      thetas: weight decay params (like betas for Adam)
      eps: ...
    """
    
    defaults = dict(betas=thetas, eps=eps)
    # params = model.parameters()
    super(DRSOMB, self).__init__(params, defaults)
    ##########################
    # AD hvps
    ##########################
    self.Hv = None
    self._params = self.get_params()
    for p in self._params:
      # keep momentum
      for k in DRSOM_DIRECTIONS:
        self.state[p][k] = torch.zeros_like(p.data, requires_grad=True)
    
    #
    self._numel_cache = None
    ##########################
    # DRSOM only params
    ##########################
    # frequency to update Hv (Hessian-vector product)
    self.freq = 1
    self._max_iter_adj = max_iter
    self.option_tr = option_tr
    
    ##########################
    # global averages & keepers
    ##########################
    self.Q: Optional[torch.Tensor] = torch.zeros(
      (2, 2), requires_grad=False, device="cpu"
    )
    self.c: Optional[torch.Tensor] = torch.zeros(
      2, requires_grad=False, device="cpu"
    )
    self.G: Optional[torch.Tensor] = torch.zeros(
      (2, 2), requires_grad=False, device="cpu"
    )
    
    ##########################
    # scalar attrs
    ##########################
    # total number of runs acc. all steps
    self.iter = 0
    self.alpha: Optional[torch.TensorType] = None
    self.alpha_norm = 0.1
    self.alpha_max = 1e4
    ##########################
    # TRS/Regularization params
    ##########################
    # gamma & lower bound on gamma
    self.gamma = gamma
    self.gammalb = 1e-12
    # maximum step size
    self.radius = 1e1
    self.radiusub = 1e1
    # decay rules
    self.decay_window = decay_window
    self.decay_step = decay_step
    self.decay_radius_step = decay_radius_step
    # gamma increasing rules
    self.beta1 = beta1
    self.beta2 = beta2
    ##########################
    # step acc rules
    ##########################
    self.eta = 0.08
    self.zeta1 = 0.25
    self.zeta2 = 0.75
    ##########################
    # weight decay of the past
    ##########################
    # other indicators
    self.ghg = 0.0
    # structured log line
    self.logline = {}
    #########################
  
  def get_name(self):
    return f"drsom-b:qp@{DRSOM_MODE_QP}d@{DRSOM_MODE}.{self.option_tr}:ad@{DRSOM_MODE_HVP}"
  
  def get_params(self):
    """
    gets all parameters in all param_groups with gradients requirements
    """
    return [
      p for group in self.param_groups for p in group["params"] if p.requires_grad
    ]
  
  def _clone_param(self):
    return [p.clone(memory_format=torch.contiguous_format) for p in self._params]
  
  @torch.no_grad()
  def _set_param(self, params_data):
    for p, pdata in zip(self._params, params_data):
      p.copy_(pdata)
  
  @torch.no_grad()
  def _use_new_d(self, d_new):
    for p in self._params:
      p.add_(d_new[p])
  
  def _bool_grad_vanish(self, p):
    return p.grad is None or torch.linalg.norm(p.grad) < 1e-8
  
  @torch.no_grad()
  def _clear_momentum(self):
    # only has globally state
    for p in self._params:
      for k in DRSOM_DIRECTIONS:
        if k in self.state[p]:
          self.state[p][k].zero_()
  
  @drsom_timer
  def _save_momentum(self, vlist, key):
    """
    saving momentum
    """
    with torch.no_grad():
      for idx, p in enumerate(self._params):
        self.state[p][key] = vlist[p]
  
  @torch.no_grad()
  def solve_alpha(self, Q, c, tr):
    # initialization
    return TRS._solve_alpha(self, Q, c, tr)
  
  @drsom_timer
  def compute_step(self, option_tr='p'):
    dim = len(self.c)
    self.alpha, self.alpha_norm = self.solve_alpha(
      self.Q, self.c, tr=self.G if option_tr == 'p' else torch.eye(dim)
    )
    
    ####################################
    # compute estimate decrease
    ####################################
    trs_est = - 1 / 2 * (self.Q @ self.alpha).dot(self.alpha) - self.c.dot(self.alpha)
    if DRSOM_VERBOSE:
      self.logline["Δ"] = "{:+.2e}".format(self.radius)
      self.logline["δ"] = "{:+.2e}".format(self.alpha_norm)
    return trs_est
  
  @drsom_timer
  def hv(self, directions):
    """
    exact metric
    Args:
      p:
      v:

    Returns:

    """
    
    gv = [torch.mul(p.grad, directions[p]).sum() for p in self._params]
    return torch.autograd.grad(
      gv, self._params,
      create_graph=True,
      retain_graph=True
    )
  
  @drsom_timer
  def compute_Q_via_hvp(self, directions, style):
    dim = len(directions)
    Q = torch.zeros((dim, dim), requires_grad=False, device="cpu")
    if self.Hv is None:
      self.Hv = [[] for _ in range(dim)]
    for i in range(dim):
      if style == 0:
        self.Hv[i] = self.hv(directions[i])
      elif style == 1:
        raise ValueError(
          "the finite diff option is unused,"
          " try DRSOM_MODE_HVP=0"
        )
      else:
        raise ValueError(
          "not implemented,"
          " try DRSOM_MODE_HVP=0"
        )
    for i in range(dim):
      for j in range(i, dim):
        for idx, p in enumerate(self._params):
          u = directions[i][p]
          hv = self.Hv[j][idx]
          Q[i, j] += torch.mul(u, hv).sum().cpu().detach()
    for i in range(dim):
      for j in range(i):
        Q[i, j] = Q[j, i]
    return Q
  
  @drsom_timer
  def build_natural_basis(self, v):
    """
      function build_natural_basis(v)
          _len = length(v)
          a = [i == j ? v[i] * v[j] / 2 : v[i] * v[j]
               for i = 1:_len
               for j = i:_len]
          return a
      end
    Returns:
    """
    _len = len(v)
    a = [v[i] * v[j] / 2 if i == j else v[i] * v[j]
         for i in range(0, _len)
         for j in range(i, _len)]
    return a
  
  @drsom_timer
  def compute_Q_via_interpolation(
      self, directions, fx, c: Optional[torch.Tensor], p_copy=None, closure=None,
      delta=1e-2, multiples=1):
    dim = len(directions)
    
    def coordinates(dim, k, scaler=1.0):
      """
      return e_k
      Args:
        dim:
        k:
      """
      arr = np.zeros(dim)
      arr[k] = scaler
      return arr
    
    def sampling():
      # trial step in the ellipsoid
      a2 = [coordinates(dim, j, np.exp(-s) * delta) for j in range(dim) for s in range(k_diag)]
      a = [np.random.uniform(0, 1, size=dim) * delta for _ in range(k_offdiag)]
      a = [*a2, *a]
      return a
    
    ###########################################################
    # compare with HVP
    # _ = closure(backward=True)
    # Q1 = self.compute_Q_via_hvp(directions, style=0)
    # q1 = Q1.triu().cpu().detach().numpy()
    # q1 = q1[q1.nonzero()]
    ###########################################################
    # we compute k > dim*(dim+1)/2 directions so that
    #   Q can be determined by solving a linear system;
    #   to this end we have to compute r*(r+1)/2 forward pass.
    # we determine the step-sizes by using the last step length
    #   and randomly select on the sphere.
    k_offdiag = int(dim * (dim - 1) / 2 * multiples)
    k_diag = int(multiples * 2)
    c_arr = c.numpy()
    a = sampling()
    
    # evaluation
    ff = [np.dot(v, c_arr) for v in a]
    k = len(a)
    df = np.zeros((k, 1))
    K = np.array([self.build_natural_basis(v) for v in a])
    d_new = {p: torch.zeros_like(p, requires_grad=False) for p in self._params}
    for j in range(k):
      for p in self._params:
        d_new[p].zero_()
        for i in range(dim):
          u = directions[i][p]
          d_new[p].add_(u, alpha=a[j][i])
      
      self._use_new_d(d_new)
      loss_est = closure(backward=False).detach().cpu()
      df[j] = loss_est - fx - ff[j]
      # set back
      self._set_param(p_copy)
    
    m, n = K.shape
    if m == n:
      q = np.linalg.solve(K, df)
    else:
      q, *_ = np.linalg.lstsq(K, df)
    q = q.flatten()
    Q = torch.zeros((dim, dim), requires_grad=False, device="cpu")
    for i in range(dim):
      for j in range(i, dim):
        Q[i, j] = q[int(j * (j + 1) / 2) + i]
    for i in range(dim):
      for j in range(0, i):
        Q[i, j] = Q[j, i]
    del d_new
    
    return Q
  
  @drsom_timer
  def update_trust_region(self, p_copy, directions, fx=0.0, closure=None, style=DRSOM_MODE_HVP):
    
    __unused = p_copy
    # each direction is a list of tensors
    dim = len(directions)
    # construct G (the inner products)
    G = torch.zeros((dim, dim), requires_grad=False, device="cpu")
    for i in range(dim):
      for j in range(i, dim):
        for p in self._params:
          v = directions[i][p]
          u = directions[j][p]
          # compute G[i,j]
          G[i, j] += torch.mul(u, v).sum().cpu().detach()
    
    # keep symmetry
    for i in range(dim):
      for j in range(i):
        G[i, j] = G[j, i]
    
    # construct c
    c = torch.zeros(dim, requires_grad=False, device="cpu")
    for i in range(dim):
      for p in self._params:
        u = directions[i][p]
        c[i] += torch.mul(p.grad, u).sum().cpu().detach()
    
    # compute Hv for v in directions;
    #   assume directions[0] = g/|g|
    if self.iter % self.freq == 0:
      if DRSOM_MODE_QP == 0:
        with torch.enable_grad():
          Q = self.compute_Q_via_hvp(directions, style)
          self.zero_grad()
      elif DRSOM_MODE_QP == 1:
        Q = self.compute_Q_via_interpolation(
          directions,
          p_copy=p_copy,
          closure=closure,
          fx=fx,
          c=c,
          delta=max(1e-3, min(self.alpha_norm, 1e1)),
        )
      else:
        raise ValueError("not handled yet")
    else:
      # if set freq = 1
      #   this never happens
      # Q = torch.tensor([[G, 0.0], [0.0, dd]], requires_grad=False)
      raise ValueError("not handled yet")
    
    self.ghg = (Q[0, 0] + self.ghg * self.iter) / (self.iter + 1)
    
    # compute Q/c/G
    self.Q = Q
    self.c = c
    # use generalized a'Ga <= delta
    self.G = G
  
  @drsom_timer
  def normalize(self, direction):
    v_norm = torch.sqrt(sum(torch.linalg.norm(v) ** 2 for p, v in direction.items()))
    v_norm = 1 if v_norm == 0 else v_norm
    return {p: v / v_norm for p, v in direction.items()}
  
  def gather_normalized_grad(self, bool_normalized=False, alpha=1):
    if bool_normalized:
      return self.normalize(
        {p: alpha * p.grad.detach().clone() for p in self._params}
      )
    return {p: alpha * p.grad.detach().clone() for p in self._params}
  
  def gather_normalize(self, k, bool_normalized=False):
    if bool_normalized:
      return self.normalize({p: self.state[p][k] for p in self._params})
    return {p: self.state[p][k] for p in self._params}
  
  def step(self, closure=None):
    """
    Performs a single optimization step.
    Arguments:
        closure (callable, optional) -- a closure that reevaluates the model and returns the loss (default: None)
    """
    
    if closure is None:
      raise ValueError("must provide a closure for DRSOM")
    closure = torch.enable_grad()(closure)
    if DRSOM_VERBOSE:
      torch.autograd.set_detect_anomaly(True)
    n_iter = 0
    if ((self.iter + 1) % self.decay_window) == 0:
      self.gammalb *= self.decay_step
      self.radiusub *= self.decay_radius_step
      print(f"adjust regularization terms@: {self.iter} and {self.decay_window}")
      print(f"current regularization terms: {self.gammalb} and {self.radiusub}")
    if self.iter == 0:
      print(f"adjust regularization terms@: {self.iter} and {self.decay_window}")
      print(f"current regularization terms: {self.gammalb} and {self.radiusub}")
    
    loss = closure()
    
    # copy of it at last step
    p_copy = self._clone_param()
    
    # @note
    # no need to scale (even for gradient) since it is a new tensor.
    g_old = self.gather_normalized_grad(alpha=-1)
    directions = [
      g_old,  # make sure -g is the first direction
      *(self.gather_normalize(k) for k in DRSOM_DIRECTIONS),
    ]
    self.update_trust_region(
      p_copy,
      directions,
      fx=loss.cpu().detach(),
      closure=closure,
      style=DRSOM_MODE_HVP,
    )
    # accept or not?
    acc_step = False
    # adjust lambda: (and thus trust region radius)
    iter_adj = 1
    with torch.no_grad():
      while iter_adj < self._max_iter_adj:
        # solve alpha
        trs_est = self.compute_step(option_tr=self.option_tr)
        if trs_est < 0:
          self.gamma = max(self.gamma * self.beta1, 1e-4)
          self.radius = max(self.radius / np.sqrt(self.beta1), 1e-12)
          if DRSOM_VERBOSE:
            pprint(self.logline)
          continue
        alpha = self.alpha
        
        # build direction
        dim = len(directions)
        d_new = {p: torch.zeros_like(p, requires_grad=False) for p in self._params}
        for p in self._params:
          for i in range(dim):
            u = directions[i][p]
            d_new[p].add_(u, alpha=alpha[i])
        
        self._use_new_d(d_new)
        loss_est = closure(backward=False).detach().cpu()
        # accept or not？
        loss_dec = loss - loss_est
        rho = loss_dec / trs_est
        
        # update the trust-region radius (implicitly by gamma/lambda)
        lmb_dec = 0
        gamma_old = self.gamma
        if rho <= self.zeta1:
          # be more conservative
          self.gamma = max(self.gamma * self.beta1, 1e-4)
          self.radius = max(self.radius * self.decay_radius_step, 1e-8)
        # @note:
        # do we need "not very successful" adjustments?
        # elif self.zeta1 < rho < self.zeta2:
        #   self.gamma = self.gamma = max(
        #     self.gammalb,
        #     self.gamma / self.beta2
        #   )
        else:
          if rho >= self.zeta2:
            # be more aggressive
            lmb_dec = 1
            self.gamma = max(
              self.gammalb,
              min(self.gamma / self.beta2, np.log(self.gamma)),
            )
            # cannot exceed radius max
            self.radius = min(
              self.radius / self.decay_radius_step,
              self.radiusub
            )
        
        acc_step = rho > self.eta
        if DRSOM_VERBOSE:
          self.logline["dQ"] = "{:+.2e}".format(trs_est.item())
          self.logline["df"] = "{:+.2e}".format(loss_dec.item())
          self.logline["rho"] = "{:+.2e}".format(rho.item())
          self.logline["acc"] = int(acc_step.item())
          self.logline["acc-𝜆"] = lmb_dec
          self.logline["𝛄"] = "{:+.2e}".format(self.gamma)
          self.logline["𝛄-"] = "{:+.2e}".format(gamma_old)
          self.logline["f"] = "{:+.2e}".format(loss.item())
          self.logline["k"] = "{:+6d}".format(self.iter)
          self.logline["k0"] = iter_adj
          print(
            pd.DataFrame(
              data=[list(self.logline.values())],
              columns=self.logline.keys(),
              dtype=str,
            ).to_markdown(
              tablefmt="grid"
            )
          )
        if not acc_step:
          # set back to old ~ trial step failed
          self._set_param(p_copy)
        
        else:
          if "momentum_g" in DRSOM_DIRECTIONS:
            # compute momentum_g
            _loss = closure()
            raise ValueError("not implemented")
          elif "momentum" in DRSOM_DIRECTIONS:
            self._save_momentum(d_new, "momentum")
          else:
            # no momentum has to be kept
            pass
          break
        
        iter_adj += 1
      
      self.iter += 1
      n_iter += 1
      
      if not acc_step:
        # if this step is not acc. (after max # of iteration for adjustment)
        # consider restart the optimizer by clearing the momentum,
        #   just like a nonlinear conjugate gradient method.
        self._clear_momentum()
        self.gamma = self.gammalb
    
    return loss
