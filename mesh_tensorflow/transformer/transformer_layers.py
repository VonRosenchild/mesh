# coding=utf-8
# Copyright 2019 The Mesh TensorFlow Authors.
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

"""Layers for the Transformer."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import math
import gin

import mesh_tensorflow as mtf
from mesh_tensorflow import layers
from mesh_tensorflow.transformer import attention
from mesh_tensorflow.transformer import transformer

import tensorflow.compat.v1 as tf


@gin.configurable
class DenseReluDense(transformer.TransformerLayer):
  """Two fully-connected layers with feed-forward activation."""

  def __init__(self, hidden_size=4096, dropout_rate=0.0):
    """Create a DenseReluDense.

    Args:
      hidden_size: an integer - size of the hidden layer
      dropout_rate: a floating-point number
    """
    self.hidden_size = hidden_size
    self.dropout_rate = dropout_rate

  def call(self, context, x, losses=None):
    """Call the layer."""
    io_channels = x.shape.dims[-1]
    hidden_channels = mtf.Dimension("d_ff", self.hidden_size)
    if context.model.ensemble_dim:
      expert_dims = [context.model.ensemble_dim]
    else:
      expert_dims = None
    h = mtf.layers.dense(x, hidden_channels,
                         use_bias=False, activation=mtf.relu,
                         variable_dtype=context.variable_dtype,
                         name="wi", expert_dims=expert_dims)
    if context.train and self.dropout_rate != 0.0:
      h = mtf.dropout(h, 1.0 - self.dropout_rate,
                      noise_shape=h.shape - context.length_dim)
    return mtf.layers.dense(h, io_channels, use_bias=False, activation=None,
                            variable_dtype=context.variable_dtype,
                            name="wo", expert_dims=expert_dims)


def attention_params(context,
                     kv_dim,
                     num_heads,
                     num_memory_heads=0,
                     shared_kv=False):
  """Attention Parameters for Transformer Layers.

  The num_heads argument indicates the number of read-heads.

  For the familiar behavior described in "Attention Is All You Need", set
  num_memory_heads=0.

  If num_memory_heads==1, then there is only a single write-head, and multiple
  read-heads.  This leads to faster incremental decoding, since the
  recurrent state is smaller

  If num_memory_heads > 1, then num_memory_heads indicates the number of
  write-heads.  A fraction of the read-heads read each write-head.
  num_memory_heads must divide num_heads. This behavior has not yet been tested.

  Args:
    context: a transformer.Context
    kv_dim: a dimension (for key and value channels)
    num_heads: an integer
    num_memory_heads: an optional integer
    shared_kv: a boolean
  Returns:
    an attention.AttentionParams object
  """
  if num_heads == 1:
    query_heads_dims = None
    memory_heads_dims = None
  elif num_memory_heads == 0:
    query_heads_dims = [mtf.Dimension("heads", num_heads)]
    memory_heads_dims = query_heads_dims
  elif num_memory_heads == 1:
    query_heads_dims = [mtf.Dimension("heads", num_heads)]
    memory_heads_dims = None
  else:
    if num_heads % num_memory_heads != 0:
      raise ValueError("num_memory_heads must divide num_heads")
    memory_heads_dims = [mtf.Dimension("heads", num_memory_heads)]
    query_heads_dims = memory_heads_dims + [
        mtf.Dimension("query_heads", num_heads // num_memory_heads)]
  return attention.AttentionParams(
      context.mesh,
      query_input_dim=context.model.model_dim,
      memory_input_dim=context.model.model_dim,
      output_dim=context.model.model_dim,
      key_dim=kv_dim,
      value_dim=kv_dim,
      query_heads_dims=query_heads_dims,
      memory_heads_dims=memory_heads_dims,
      variable_dtype=context.variable_dtype,
      shared_kv=shared_kv,
      ensemble_dim=context.model.ensemble_dim)


@gin.configurable
class SelfAttention(transformer.TransformerLayer):
  """Multi-head self-attention layer."""

  def __init__(self,
               num_heads=8,
               num_memory_heads=0,
               key_value_size=128,
               shared_kv=False,
               dropout_rate=0.0,
               attention_kwargs=None,
               relative_attention_type=None,
               relative_attention_num_buckets=32):
    """Create a SelfAttention Layer.

    Args:
      num_heads: an integer
      num_memory_heads: an optional integer
      key_value_size: an integer
      shared_kv: a boolean
      dropout_rate: a float
      attention_kwargs: a dictionary of kwargs for attention.attention
      relative_attention_type: an optional string - one of
        (None, "bias", "bias_shared", "contextual")
      relative_attention_num_buckets: an integer
    """
    self.num_heads = num_heads
    self.num_memory_heads = num_memory_heads
    self.key_value_size = key_value_size
    self.shared_kv = shared_kv
    self.dropout_rate = dropout_rate
    self.attention_kwargs = attention_kwargs or {}
    self.relative_attention_type = relative_attention_type
    self.relative_attention_num_buckets = relative_attention_num_buckets

  def attention_kwargs_from_context(self, context):
    kwargs = copy.copy(self.attention_kwargs)
    kwargs["dropout_rate"] = self.dropout_rate if context.train else 0.0
    if "dropout_broadcast_dims" not in kwargs:
      kwargs["dropout_broadcast_dims"] = [context.length_dim]
    return kwargs

  def make_params(self, context):
    return attention_params(context=context,
                            kv_dim=self.kv_dim,
                            num_heads=self.num_heads,
                            num_memory_heads=self.num_memory_heads,
                            shared_kv=self.shared_kv)

  def call(self, context, x, losses=None):
    """Call the layer."""
    params = self.make_params(context)
    q = params.compute_q(x)
    memory_length = self.memory_length(context)
    if context.mode == "incremental":
      m = x
    else:
      m = mtf.replace_dimensions(x, context.length_dim, memory_length)
    if self.shared_kv:
      kv = params.compute_kv(m)
    else:
      k = params.compute_k(m)
      v = params.compute_v(m)
    if context.mode == "incremental":
      one_hot = mtf.one_hot(
          context.position, memory_length, dtype=context.activation_dtype)
      inv_one_hot = 1.0 - one_hot
      if self.shared_kv:
        old_kv = context.get_states(1)
        kv = old_kv * inv_one_hot + kv * one_hot
      else:
        old_k, old_v = context.get_states(2)
        k = old_k * inv_one_hot + k * one_hot
        v = old_v * inv_one_hot + v * one_hot
      memory_position = mtf.range(context.mesh, memory_length, tf.int32)
    else:
      memory_position = self.rename_length_to_memory_length(
          context.position, context)
    if context.mode == "incremental" or context.mode == "first_part":
      context.record_new_states([kv] if self.shared_kv else [k, v])
    if self.shared_kv:
      k = kv
      v = kv
    o = attention.attention(
        q, k, v,
        memory_length,
        self.kv_dim,
        self.kv_dim,
        self.compute_bias(context, memory_position, x),
        **self.attention_kwargs_from_context(context))
    return params.compute_output(o, output_shape=x.shape)

  def compute_bias(self, context, memory_position, x):
    """Compute attention bias.

    Args:
      context: a transformer.Context
      memory_position: an int32 tensor containing memory_length dimension.
      x: a Tensor - the query antecedent - required for relative attention
    Returns:
      a Tensor or None
    """
    min_relative_position = self.min_relative_position(context)
    max_relative_position = self.max_relative_position(context)
    # we can often cache the result of this function between similar layers
    can_cache = (
        self.relative_attention_type is None or
        self.relative_attention_type == "bias_shared")
    if can_cache:
      cache_key = ("self_attention_mask",
                   min_relative_position,
                   max_relative_position,
                   self.relative_attention_type,
                   self.num_heads)
      if cache_key in context.cache:
        return context.cache[cache_key]
    biases = []
    relative_position = memory_position - context.position
    if min_relative_position is not None:
      visible = mtf.greater_equal(relative_position, min_relative_position)
      biases.append(attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype))
    if max_relative_position is not None:
      visible = mtf.less_equal(relative_position, max_relative_position)
      biases.append(attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype))
    if context.read_priority is not None:
      visible = mtf.greater_equal(
          context.read_priority,
          mtf.layers.rename_length_to_memory_length(context.write_priority))
      biases.append(attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype))

    sequence_id = None
    # Subsequence id should only be set if we are in the decoder and have
    # multiple targets per input. This will allow each sub-target to only attend
    # to itself.
    if isinstance(context.subsequence_id, mtf.Tensor):
      sequence_id = context.subsequence_id
    elif isinstance(context.sequence_id, mtf.Tensor):
      sequence_id = context.sequence_id
    if (sequence_id is not None and context.length_dim in sequence_id.shape):
      visible = mtf.equal(
          sequence_id,
          self.rename_length_to_memory_length(sequence_id, context))
      biases.append(attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype))
    if self.relative_attention_type is not None:
      buckets_dim = mtf.Dimension(
          "buckets", self.relative_attention_num_buckets)
      heads_dim = mtf.Dimension("heads", self.num_heads)
      bidirectional = not context.model.fully_autoregressive
      rp_bucket = _relative_position_bucket(
          relative_position,
          bidirectional=bidirectional,
          num_buckets=buckets_dim.size)
      if (self.relative_attention_type == "bias" or
          self.relative_attention_type == "bias_shared"):
        bias_shape = [heads_dim, buckets_dim]
        if context.model.ensemble_dim:
          bias_shape = [context.model.ensemble_dim] + bias_shape
        values = mtf.get_variable(
            context.mesh, "relative_attention_bias",
            bias_shape, dtype=context.variable_dtype)
      elif self.relative_attention_type == "contextual":
        if context.model.ensemble_dim:
          expert_dims = [context.model.ensemble_dim]
        else:
          expert_dims = None
        values = layers.dense(
            x, [buckets_dim, heads_dim],
            variable_dtype=context.variable_dtype,
            name="relative_attention_contextual",
            expert_dims=expert_dims)
      else:
        raise ValueError("unrecognized relative_attention_type \"%s\"" %
                         self.relative_attention_type)
      biases.append(mtf.gather(values, rp_bucket, buckets_dim))
    ret = mtf.add_n(biases) if biases else None
    if can_cache:
      context.cache[cache_key] = ret
    return ret

  @property
  def kv_dim(self):
    return mtf.Dimension("d_kv", self.key_value_size)

  def memory_length(self, context):
    return mtf.Dimension("memory_length", context.length_dim.size)

  def rename_length_to_memory_length(self, x, context):
    return mtf.replace_dimensions(
        x, context.length_dim, self.memory_length(context))

  def min_relative_position(self, context):
    return None

  def max_relative_position(self, context):
    return None


@gin.configurable
class EncDecAttention(SelfAttention):
  """Multi-head attention over encoder output."""

  def _get_memory_antecedent(self, context):
    return context.encoder_output

  def call(self, context, x, losses=None):
    """Call the layer."""
    memory_antecedent = self._get_memory_antecedent(context)
    memory_input_dim = memory_antecedent.shape[-1]
    if memory_input_dim != context.model.model_dim:
      raise NotImplementedError(
          "TODO(noam): support different model_dim in encoder and decoder.")
    params = self.make_params(context)
    q = params.compute_q(x)
    if context.mode == "incremental":
      k, v, memory_length = context.get_constant_state()
    else:
      m = memory_antecedent
      if self.shared_kv:
        kv = params.compute_kv(m)
        k = kv
        v = kv
      else:
        k = params.compute_k(m)
        v = params.compute_v(m)
      memory_length, = [d for d in m.shape.dims if d.name == "memory_length"]
      if context.mode == "first_part":
        context.record_constant_state((k, v, memory_length))
    if context.encoder_sequence_id and context.sequence_id:
      visible = mtf.equal(context.sequence_id, context.encoder_sequence_id)
      bias = attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype)
    else:
      bias = None
    o = attention.attention(
        q, k, v,
        memory_length,
        self.kv_dim,
        self.kv_dim,
        bias,
        **self.attention_kwargs_from_context(context))
    return params.compute_output(o, output_shape=x.shape)


@gin.configurable
class TransparentEncDecAttention(EncDecAttention):
  """Transparent multi-head attention over encoder output."""

  def __init__(self,
               layers_per_encoder_module=gin.REQUIRED,
               layers_per_decoder_module=gin.REQUIRED,
               encoder_num_modules=gin.REQUIRED,
               decoder_num_modules=gin.REQUIRED,
               dropout_rate=0.0,
               **kwargs):
    """Create a transparent attention EncDec Layer.

    Args:
      layers_per_encoder_module: positive integer telling how many layer are in
        each repeated module in the encoder
      layers_per_decoder_module: positive integer telling how many layer are in
        each repeated module in the decoder
      encoder_num_modules: positive integer of how many repeated modules there
        are in the encoder
      decoder_num_modules: positive integer of how many repeated modules there
        are in the decoder
      dropout_rate: positive float, the dropout rate for the matrix relating
        encoder outputs to decoder inputs
      **kwargs: additional constructor params
    """
    super(TransparentEncDecAttention, self).__init__(**kwargs)
    self.layers_per_encoder_module = layers_per_encoder_module
    self.layers_per_decoder_module = layers_per_decoder_module
    self.encoder_num_modules = encoder_num_modules
    self.decoder_num_modules = decoder_num_modules
    self.dropout_rate = dropout_rate

  def _get_memory_antecedent(self, context):
    decoder_module_index = context.layer_index // self.layers_per_decoder_module
    decoder_inputs = self._get_decoder_inputs(context)
    return decoder_inputs[decoder_module_index]

  def _get_decoder_inputs(self, context):
    """Computes the inputs to the decoder when using transparent attention.

    We must cache on the context in order to ensure that we are not replicating
    variables when the layer's call function is called in different tf variable
    scopes.

    Args:
      context: a Context

    Returns:
      a list containing `self.num_decoder_modules` of tensors with shape
        [<batch_dims>, length_dim, output_vocab_dim]
    """
    if hasattr(context, "decoder_layers_per_module"):
      return context.decoder_layers_per_module

    encoder_layer_outputs = [
        mtf.layers.rename_length_to_memory_length(output)
        for output in context.encoder_layer_outputs
    ]

    layers_per_module = self.layers_per_encoder_module
    encoder_module_outputs_dim = mtf.Dimension(
        "encoder_module_outputs", size=self.encoder_num_modules + 1)
    decoder_module_inputs_dim = mtf.Dimension(
        "decoder_module_inputs", size=self.decoder_num_modules)
    encoder_module_outputs = mtf.stack(
        [encoder_layer_outputs[0]] +
        encoder_layer_outputs[layers_per_module::layers_per_module],
        dim_name="encoder_module_outputs")
    w = mtf.get_variable(
        context.mesh,
        "w",
        mtf.Shape([encoder_module_outputs_dim, decoder_module_inputs_dim]),
        initializer=tf.random_normal_initializer(
            stddev=(encoder_module_outputs_dim.size *
                    decoder_module_inputs_dim.size)**-0.5),
        dtype=context.variable_dtype)
    if context.train and self.dropout_rate != 0.0:
      w = mtf.dropout(w, 1.0 - self.dropout_rate)
    s = mtf.softmax(w, reduced_dim=encoder_module_outputs_dim)
    z = mtf.einsum([s, encoder_module_outputs],
                   reduced_dims=[encoder_module_outputs_dim])
    input_per_decoder = mtf.split(
        z,
        split_dim=decoder_module_inputs_dim,
        num_or_size_splits=decoder_module_inputs_dim.size)
    context.decoder_layers_per_module = [
        mtf.reshape(inpt, z.shape.dims[1:]) for inpt in input_per_decoder
    ]
    return context.decoder_layers_per_module


@gin.configurable
class LocalSelfAttention(SelfAttention):
  """Multi-head local self-attention layer."""

  def __init__(self,
               radius=128,
               num_heads=8,
               num_memory_heads=0,
               key_value_size=128,
               shared_kv=False,
               dropout_rate=0.0,
               attention_kwargs=None,):
    super(LocalSelfAttention, self).__init__(
        num_heads,
        num_memory_heads,
        key_value_size,
        shared_kv,
        dropout_rate,
        attention_kwargs)
    self.radius = radius

  def call(self, context, x, losses=None):
    """Call the layer."""
    params = self.make_params(context)
    q = params.compute_q(x)
    if self.shared_kv:
      kv = params.compute_kv(x)
      k = kv
      v = kv
    else:
      k = params.compute_k(x)
      v = params.compute_v(x)
    if context.mode == "incremental":
      if self.shared_kv:
        prev_kv, = context.get_states(1)
      else:
        prev_k, prev_v = context.get_states(2)
      current_position = mtf.equal(
          mtf.range(context.mesh, self.window_dim, dtype=tf.int32),
          mtf.mod(context.position, self.radius))
      if self.shared_kv:
        kv = mtf.where(current_position, kv, prev_kv,
                       output_shape=prev_kv.shape)
        k = kv
        v = kv
        context.record_new_states([kv])
      else:
        k = mtf.where(current_position, params.compute_k(x), prev_k,
                      output_shape=prev_k.shape)
        v = mtf.where(current_position, params.compute_v(x), prev_v,
                      output_shape=prev_v.shape)
        context.record_new_states([k, v])
      window_pos = mtf.range(context.mesh, self.window_dim, tf.int32)
      visible = mtf.greater_equal(context.position, window_pos)
      bias = attention.visibility_mask_to_attention_bias(
          visible, context.activation_dtype)
      o = attention.attention(
          q,
          k,
          v,
          self.window_dim,
          self.kv_dim,
          self.kv_dim,
          bias,
          **self.attention_kwargs_from_context(context))
    elif context.length_dim.size <= max(256, self.radius * 4):
      # nothing fancy - just do full attention and mask
      memory_length = self.rename_length_to_memory_length(
          context.position, context)
      o = attention.attention(
          q,
          self.rename_length_to_memory_length(k, context),
          self.rename_length_to_memory_length(v, context),
          self.memory_length(context),
          self.kv_dim,
          self.kv_dim,
          self.compute_bias(context, memory_length, x),
          **self.attention_kwargs_from_context(context))
    else:
      # fancy local attention algorithm
      o = attention.local_attention_1d(
          q=q,
          k=k,
          v=None if self.shared_kv else v,
          length_dim=context.length_dim,
          key_dim=self.kv_dim,
          value_dim=self.kv_dim,
          length_dim_num_splits=1,  # TODO(noam): look at the layout
          autoregressive=context.model.fully_autoregressive,
          radius=self.radius,
          sequence_id=context.sequence_id,
          write_priority=context.write_priority,
          read_priority=context.read_priority,
          attention_kwargs=self.attention_kwargs_from_context(context))
    if context.mode == "first_part":
      window_pos = mtf.range(context.mesh, self.window_dim, tf.int32)
      pos = mtf.range(context.mesh, context.length_dim, tf.int32)
      select_recent = mtf.cast(
          mtf.equal(mtf.mod(pos, self.radius), window_pos), x.dtype)
      select_recent *= mtf.cast(
          mtf.less(pos, context.initial_position), x.dtype)
      select_recent *= mtf.cast(
          mtf.greater_equal(
              pos, context.initial_position - self.radius), x.dtype)
      state_shape = (k.shape - [context.length_dim, self.kv_dim]
                     + [self.window_dim, self.kv_dim])
      k_state = mtf.einsum(
          [k, select_recent], output_shape=state_shape,
          reduced_dims=[context.length_dim])
      context.new_states.append(k_state)
      if not self.shared_kv:
        v_state = mtf.einsum(
            [v, select_recent], output_shape=state_shape,
            reduced_dims=[context.length_dim])
        context.new_states.append(v_state)
    return params.compute_output(o, output_shape=x.shape)

  def min_relative_position(self, context):
    return 1 - self.radius

  def max_relative_position(self, context):
    return None if context.model.fully_autoregressive else self.radius

  @property
  def window_dim(self):
    return mtf.Dimension("window", self.radius)


def _relative_position_bucket(relative_position,
                              bidirectional=True,
                              num_buckets=32,
                              max_distance=128):
  """Translate relative position to a bucket number for relative attention.

  The relative position is defined as memory_position - query_position, i.e.
  the distance in tokens from the attending position to the attended-to
  position.  If bidirectional=False, then positive relative positions are
  invalid.

  We use smaller buckets for small absolute relative_position and larger buckets
  for larger absolute relative_positions.  All relative positions >=max_distance
  map to the same bucket.  All relative positions <=-max_distance map to the
  same bucket.  This should allow for more graceful generalization to longer
  sequences than the model has been trained on.

  Args:
    relative_position: an int32 Tensor
    bidirectional: a boolean - whether the attention is bidirectional
    num_buckets: an integer
    max_distance: an integer
  Returns:
    a Tensor with the same shape as relative_position, containing int32
      values in the range [0, num_buckets)
  """
  ret = 0
  n = -relative_position
  if bidirectional:
    num_buckets //= 2
    ret += mtf.to_int32(mtf.less(n, 0)) * num_buckets
    n = mtf.abs(n)
  else:
    n = mtf.maximum(n, 0)
  # now n is in the range [0, inf)
  max_exact = num_buckets // 2
  is_small = mtf.less(n, max_exact)
  val_if_large = max_exact + mtf.to_int32(
      mtf.log(mtf.to_float(n) / max_exact)
      / math.log(max_distance / max_exact) * (num_buckets - max_exact))
  val_if_large = mtf.minimum(val_if_large, num_buckets - 1)
  ret += mtf.where(is_small, n, val_if_large)
  return ret
