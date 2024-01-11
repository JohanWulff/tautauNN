# coding: utf-8

from __future__ import annotations

import os
import re
import json
import time
import pickle
import shutil
import hashlib
from collections import defaultdict
from getpass import getuser
from copy import deepcopy
from typing import Any

import numpy as np
import tensorflow as tf
from law.util import human_duration
from tabulate import tabulate

from tautaunn.multi_dataset import MultiDataset
from tautaunn.tf_util import (
    get_device, ClassificationModelWithValidationBuffers, L2Metric, ReduceLRAndStopAndRepeat, EmbeddingEncoder,
    LivePlotWriter,
)
from tautaunn.util import load_sample_root, calc_new_columns, create_model_name, transform_data_dir_cache
from tautaunn.config import (
    Sample, activation_settings, dynamic_columns, embedding_expected_inputs, regression_sets, cont_feature_sets,
    cat_feature_sets,
)


this_dir = os.path.dirname(os.path.realpath(__file__))

# whether to use a gpu
use_gpu: bool = True
# forces deterministic behavior on gpus, which can be slower, but it is observed on some gpus that weird numeric effects
# can occur (e.g. all batches are fine, and then one batch leads to a tensor being randomly transposed, or operations
# not being applied at all), and whether the flag is needed or not might also depend on the tf and cuda version
deterministic_ops: bool = True
# run in eager mode (for proper debuggin, also consider decorating methods in question with @util.debug_layer)
eager_mode: bool = False
# whether to jit compile via xla (not working on GPU right now)
jit_compile: bool = False
# limit the cpu to a reduced number of threads
limit_cpus: bool | int = False
# profile the training
run_profiler: bool = False
# data directories per year
data_dirs: dict[str, str] = {
    "2016": os.environ["TN_SKIMS_2016"],
    "2016APV": os.environ["TN_SKIMS_2016APV"],
    "2017": os.environ["TN_SKIMS_2017"],
    "2018": os.environ["TN_SKIMS_2018"],
}
# cache dir for data
cache_dir: str | None = os.path.join(os.environ["TN_DATA_DIR"], "cache")
# where tensorboard logs should be written
tensorboard_dir: str | None = os.getenv("TN_TENSORBOARD_DIR", os.path.join(os.environ["TN_DATA_DIR"], "tensorboard"))
# model save dir
model_dir: str = os.getenv("TN_MODEL_DIR", os.path.join(this_dir, "models"))
# fallback model save dir (in case kerberos permissions were lost in the meantime)
model_fallback_dir: str | None = f"/tmp/{getuser()}/models"

# apply settings
device = get_device(device="gpu" if use_gpu else "cpu", num_device=0)
if use_gpu and "gpu" not in device._device_name.lower():
    use_gpu = False
if use_gpu and deterministic_ops:
    tf.config.experimental.enable_op_determinism()
if limit_cpus:
    tf.config.threading.set_intra_op_parallelism_threads(int(limit_cpus))
    tf.config.threading.set_inter_op_parallelism_threads(int(limit_cpus))
if eager_mode:
    # note: running the following with False would still trigger partial eager mode in keras
    tf.config.run_functions_eagerly(eager_mode)


def train(
    model_name: str | None = None,
    model_prefix: str = "hbtres",
    model_suffix: str = "",
    data_dirs: dict[str, str] = data_dirs,
    cache_dir: str | None = cache_dir,
    tensorboard_dir: str | None = tensorboard_dir,
    tensorboard_version: str | None = None,
    clear_existing_tensorboard: bool = True,
    model_dir: str = model_dir,
    model_fallback_dir: str | None = model_fallback_dir,
    samples: list[Sample] = [
        Sample("ggF_Radion_m320", year="2017", label=0, spin=0, mass=320.0),
        Sample("ggF_Radion_m350", year="2017", label=0, spin=0, mass=350.0),
        Sample("ggF_Radion_m400", year="2017", label=0, spin=0, mass=400.0),
        Sample("ggF_Radion_m450", year="2017", label=0, spin=0, mass=450.0),
        Sample("ggF_Radion_m500", year="2017", label=0, spin=0, mass=500.0),
        Sample("ggF_Radion_m550", year="2017", label=0, spin=0, mass=550.0),
        Sample("ggF_Radion_m600", year="2017", label=0, spin=0, mass=600.0),
        Sample("ggF_Radion_m650", year="2017", label=0, spin=0, mass=650.0),
        Sample("ggF_Radion_m700", year="2017", label=0, spin=0, mass=700.0),
        Sample("ggF_Radion_m750", year="2017", label=0, spin=0, mass=750.0),
        Sample("ggF_Radion_m800", year="2017", label=0, spin=0, mass=800.0),
        Sample("ggF_Radion_m850", year="2017", label=0, spin=0, mass=850.0),
        Sample("ggF_Radion_m900", year="2017", label=0, spin=0, mass=900.0),
        Sample("ggF_Radion_m1000", year="2017", label=0, spin=0, mass=1000.0),
        Sample("ggF_Radion_m1250", year="2017", label=0, spin=0, mass=1250.0),
        Sample("ggF_Radion_m1500", year="2017", label=0, spin=0, mass=1500.0),
        Sample("ggF_Radion_m1750", year="2017", label=0, spin=0, mass=1750.0),
        Sample("ggF_BulkGraviton_m320", year="2017", label=0, spin=2, mass=320.0),
        Sample("ggF_BulkGraviton_m350", year="2017", label=0, spin=2, mass=350.0),
        Sample("ggF_BulkGraviton_m400", year="2017", label=0, spin=2, mass=400.0),
        Sample("ggF_BulkGraviton_m450", year="2017", label=0, spin=2, mass=450.0),
        Sample("ggF_BulkGraviton_m500", year="2017", label=0, spin=2, mass=500.0),
        Sample("ggF_BulkGraviton_m550", year="2017", label=0, spin=2, mass=550.0),
        Sample("ggF_BulkGraviton_m600", year="2017", label=0, spin=2, mass=600.0),
        Sample("ggF_BulkGraviton_m650", year="2017", label=0, spin=2, mass=650.0),
        Sample("ggF_BulkGraviton_m700", year="2017", label=0, spin=2, mass=700.0),
        Sample("ggF_BulkGraviton_m750", year="2017", label=0, spin=2, mass=750.0),
        Sample("ggF_BulkGraviton_m800", year="2017", label=0, spin=2, mass=800.0),
        Sample("ggF_BulkGraviton_m850", year="2017", label=0, spin=2, mass=850.0),
        Sample("ggF_BulkGraviton_m900", year="2017", label=0, spin=2, mass=900.0),
        Sample("ggF_BulkGraviton_m1000", year="2017", label=0, spin=2, mass=1000.0),
        Sample("ggF_BulkGraviton_m1250", year="2017", label=0, spin=2, mass=1250.0),
        Sample("ggF_BulkGraviton_m1500", year="2017", label=0, spin=2, mass=1500.0),
        Sample("ggF_BulkGraviton_m1750", year="2017", label=0, spin=2, mass=1750.0),
        Sample("DY_amc_incl", year="2017", label=1),
        Sample("TT_fullyLep", year="2017", label=1),
        Sample("TT_semiLep", year="2017", label=1),
    ],
    # names of classes
    class_names: dict[int, str] = {
        0: "HH",
        1: "Background",
    },
    # additional columns to load
    extra_columns: list[str] = [
        "EventNumber", "MC_weight", "PUReweight",
    ],
    # selections to apply before training
    selections: str | list[str] | dict[str, list[str]] = [
        "nbjetscand > 1",
        "nleps == 0",
        "isOS == 1",
        "dau2_deepTauVsJet >= 5",
        (
            "((pairType == 0) & (dau1_iso < 0.15) & (isLeptrigger == 1)) | "
            "((pairType == 1) & (dau1_eleMVAiso == 1) & (isLeptrigger == 1)) | "
            "((pairType == 2) & (dau1_deepTauVsJet >= 5))"
        ),
    ],
    # categorical input features for the network
    cat_input_names: list[str] = [
        "pairType", "dau1_decayMode", "dau2_decayMode", "dau1_charge", "dau2_charge",
    ],
    # continuous input features to the network
    cont_input_names: list[str] = [
        "met_px", "met_py", "dmet_resp_px", "dmet_resp_py", "dmet_reso_px",
        "met_cov00", "met_cov01", "met_cov11",
        "ditau_deltaphi", "ditau_deltaeta",
        *[
            f"dau{i}_{feat}"
            for i in [1, 2]
            for feat in ["px", "py", "pz", "e", "dxy", "dz", "iso"]
        ],
        *[
            f"bjet{i}_{feat}"
            for i in [1, 2]
            for feat in [
                "px", "py", "pz", "e", "btag_deepFlavor", "cID_deepFlavor", "pnet_bb", "pnet_cc", "pnet_b", "pnet_c",
                "pnet_g", "pnet_uds", "pnet_pu", "pnet_undef", "HHbtag",
            ]
        ],
    ],
    # number of layers and units
    units: list[int] = [128] * 5,
    # connection type, "fcn", "res", or "dense"
    connection_type: str = "fcn",
    # dimension of the embedding layer output will be embedding_output_dim x len(cat_input_names)
    embedding_output_dim: int = 5,
    # activation function after each hidden layer
    activation: str = "elu",
    # scale for the l2 loss term (which is already normalized to the number of weights)
    l2_norm: float = 50.0,
    # dropout percentage
    dropout_rate: float = 0.0,
    # batch norm between layers
    batch_norm: bool = True,
    # batch size
    batch_size: int = 4096,
    # name of the optimizer to use
    optimizer: str = "adam",
    # learning rate to start with
    learning_rate: float = 3e-3,
    # half the learning rate if the validation loss hasn't improved in this many validation steps
    learning_rate_patience: int = 8,
    # how even the learning rate is halfed before training is stopped
    learning_rate_reductions: int = 6,
    # stop training if the validation loss hasn't improved since this many validation steps
    early_stopping_patience: int = 10,
    # maximum number of epochs to even cap early stopping
    max_epochs: int = 10000,
    # how frequently to calulcate the validation loss
    validate_every: int = 500,
    # add the year of the sample as a categorical input
    parameterize_year: bool = True,
    # add the generator spin for the signal samples as categorical input -> network parameterized in spin
    parameterize_spin: bool = True,
    # add the generator mass for the signal samples as continuous input -> network parameterized in mass
    parameterize_mass: bool = True,
    # the name of a regression config set to use
    regression_set: str | None = None,
    # number of folds
    n_folds: int = 5,
    # number of the fold to train for
    fold_index: int = 0,
    # fraction of events to use for validation, relative to number of events in the training folds
    validation_fraction: float = 0.25,
    # seed for random number generators, if None, uses fold_index + 1
    seed: int | None = None,
) -> tuple[tf.keras.Model, str] | None:
    # some checks
    assert units
    unique_labels: set[int] = {sample.label for sample in samples}
    n_classes: int = len(unique_labels)
    assert n_classes > 1
    assert len(class_names) == n_classes
    assert all(label in class_names for label in unique_labels)
    assert "spin" not in cat_input_names
    assert "mass" not in cont_input_names
    assert 0 <= fold_index < n_folds
    assert 0 < validation_fraction < 1
    assert optimizer in ["adam", "adamw"]
    assert len(samples) == len(set(samples))
    assert all(sample.year in data_dirs for sample in samples)
    regression_cfg = regression_sets[regression_set] if regression_set else None
    if regression_cfg:
        assert fold_index in regression_cfg.model_files
        assert os.path.exists(regression_cfg.model_files[fold_index])
        reg_cont_input_names = list(cont_feature_sets[regression_cfg.cont_feature_set])
        reg_cat_input_names = list(cat_feature_sets[regression_cfg.cat_feature_set])

    # conditionally change arguments
    if seed is None:
        seed = fold_index + 1

    # copy mutables to avoid side effects
    samples = deepcopy(samples)
    class_names = deepcopy(class_names)
    extra_columns = deepcopy(extra_columns)
    selections = deepcopy(selections)
    cat_input_names = deepcopy(cat_input_names)
    cont_input_names = deepcopy(cont_input_names)
    units = deepcopy(units)

    # construct a model name
    model_name = create_model_name(
        model_name=model_name,
        model_prefix=model_prefix,
        model_suffix=model_suffix,
        embedding_output_dim=embedding_output_dim,
        units=units,
        connection_type=connection_type,
        activation=activation,
        batch_norm=batch_norm,
        l2_norm=l2_norm,
        dropout_rate=dropout_rate,
        batch_size=batch_size,
        learning_rate=learning_rate,
        optimizer=optimizer,
        parameterize_year=parameterize_year,
        parameterize_spin=parameterize_spin,
        parameterize_mass=parameterize_mass,
        regression_set=regression_set,
        fold_index=fold_index,
        seed=seed,
    )

    # some logs
    print(f"building and training model {model_name}")
    if cache_dir:
        print(f"using cache directory {cache_dir}")
    print("")

    # set the seed to everything (Python, NumPy, TensorFlow, Keras)
    tf.keras.utils.set_random_seed(fold_index * 100 + seed)

    # join selections int strings, mapped to years
    years = sorted(set(sample.year for sample in samples))
    if not isinstance(selections, dict):
        selections = {year: selections for year in years}
    for year, _selections in selections.items():
        if isinstance(_selections, list):
            _selections = " & ".join(map("({})".format, _selections))
        selections[year] = _selections
    if (uncovered_years := set(years) - set(selections)):
        raise ValueError(f"selections for years {uncovered_years} are missing")

    # extend input names by that of regression
    combined_cont_input_names = list(cont_input_names)
    combined_cat_input_names = list(cat_input_names)
    if regression_cfg:
        for name in reg_cont_input_names:
            if name not in combined_cont_input_names:
                combined_cont_input_names.append(name)
        for name in reg_cat_input_names:
            if name not in combined_cat_input_names:
                combined_cat_input_names.append(name)

    # determine which columns to read
    columns_to_read = set()
    for name in combined_cont_input_names + combined_cat_input_names:
        columns_to_read.add(name)
    # column names in selections strings
    for selection_str in selections.values():
        columns_to_read |= set(re.findall(r"[a-zA-Z_][\w_]*", selection_str))
    # extra columns
    columns_to_read |= set(extra_columns)
    # expand dynamic columns, keeping track of those that are needed
    all_dyn_names = set(dynamic_columns)
    dyn_names = set()
    while (to_expand := columns_to_read & all_dyn_names):
        for name in to_expand:
            columns_to_read |= set(dynamic_columns[name][0])
        columns_to_read -= to_expand
        dyn_names |= to_expand

    # order dynamic columns to be added
    all_dyn_names = list(dynamic_columns)
    dyn_names = sorted(dyn_names, key=all_dyn_names.index)

    # get lists of embedded feature values
    possible_cat_input_values = [deepcopy(embedding_expected_inputs[name]) for name in cat_input_names]

    # scan samples and their labels to construct relative weights such that each class starts with equal importance
    labels_to_samples: dict[int, list[str]] = defaultdict(list)
    for sample in samples:
        labels_to_samples[sample.label].append(sample.name)

    # lists for collection data to be forwarded into the MultiDataset
    cont_inputs_train, cont_inputs_valid = [], []
    cat_inputs_train, cat_inputs_valid = [], []
    labels_train, labels_valid = [], []
    event_weights_train, event_weights_valid = [], []

    # keep track of yield factors
    yield_factors: dict[str, float] = {}

    # prepare fold indices to use
    train_fold_indices: list[int] = [i for i in range(n_folds) if i != fold_index]

    # helper to flatten rec arrays
    flatten_rec = lambda r, t: r.astype([(n, t) for n in r.dtype.names], copy=False).view(t).reshape((-1, len(r.dtype)))

    data_is_cached = False
    if cache_dir:
        cache_key = [
            tuple(sample.hash_values for sample in samples),
            tuple(transform_data_dir_cache(data_dirs[year]) for year in sorted(years)),
            tuple(sorted(selections[year]) for year in sorted(years)),
            sorted(columns_to_read),
            combined_cont_input_names,
            combined_cat_input_names,
            n_classes,
            parameterize_year,
            parameterize_mass,
            parameterize_spin,
            regression_set,
            n_folds,
            fold_index,
            validation_fraction,
            seed,
        ]
        cache_hash = hashlib.sha256(str(cache_key).encode("utf-8")).hexdigest()[:10]
        cache_file = os.path.join(cache_dir, f"alldata_{cache_hash}.pkl")
        data_is_cached = os.path.exists(cache_file)

    if data_is_cached:
        # read data from cache
        print(f"loading all data from {cache_file}")
        with open(cache_file, "rb") as f:
            (
                cont_inputs_train,
                cont_inputs_valid,
                cat_inputs_train,
                cat_inputs_valid,
                labels_train,
                labels_valid,
                event_weights_train,
                event_weights_valid,
                yield_factors,
            ) = pickle.load(f)

    else:
        # loop through samples
        for sample in samples:
            rec = load_sample_root(
                data_dirs[sample.year],
                sample,
                list(columns_to_read),
                selections[sample.year],
                cache_dir=cache_dir,
            )
            n_events = len(rec)

            # add dynamic columns
            rec = calc_new_columns(rec, {name: dynamic_columns[name] for name in dyn_names})

            # prepare arrays
            cont_inputs = flatten_rec(rec[combined_cont_input_names], np.float32)
            cat_inputs = flatten_rec(rec[combined_cat_input_names], np.int32)
            labels = np.zeros((n_events, n_classes), dtype=np.float32)
            labels[:, sample.label] = 1

            # add year, spin and mass if given
            if parameterize_year or (regression_cfg and regression_cfg.parameterize_year):
                cat_inputs = np.append(cat_inputs, (np.ones(n_events, dtype=np.int32) * sample.year_int)[:, None], axis=1)
            if parameterize_mass or (regression_cfg and regression_cfg.parameterize_mass):
                cont_inputs = np.append(cont_inputs, (np.ones(n_events, dtype=np.float32) * sample.mass)[:, None], axis=1)
            if parameterize_spin or (regression_cfg and regression_cfg.parameterize_spin):
                cat_inputs = np.append(cat_inputs, (np.ones(n_events, dtype=np.int32) * sample.spin)[:, None], axis=1)

            # lookup all number of events used during training using event number and fold indices
            last_digit = rec["EventNumber"] % n_folds
            all_train_indices = np.where(np.any(last_digit[..., None] == train_fold_indices, axis=1))[0]
            # randomly split according to validation_fraction into actual training and validation indices
            valid_indices = np.random.choice(
                all_train_indices,
                size=int(len(all_train_indices) * validation_fraction),
                replace=False,
            )
            train_indices = np.setdiff1d(all_train_indices, valid_indices)

            # fill dataset lists
            cont_inputs_train.append(cont_inputs[train_indices])
            cont_inputs_valid.append(cont_inputs[valid_indices])

            cat_inputs_train.append(cat_inputs[train_indices])
            cat_inputs_valid.append(cat_inputs[valid_indices])

            labels_train.append(labels[train_indices])
            labels_valid.append(labels[valid_indices])

            event_weights = np.array([sample.loss_weight] * len(rec), dtype="float32")
            event_weights_train.append(event_weights[train_indices][..., None])
            event_weights_valid.append(event_weights[valid_indices][..., None])

            # store the yield factor for later use
            yield_factors[sample.name] = (rec["PUReweight"] * rec["MC_weight"] / rec["sum_weights"]).sum()

        if cache_dir:
            # cache data
            print(f"caching all data to {cache_file}")
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "wb") as f:
                pickle.dump(
                    (
                        cont_inputs_train,
                        cont_inputs_valid,
                        cat_inputs_train,
                        cat_inputs_valid,
                        labels_train,
                        labels_valid,
                        event_weights_train,
                        event_weights_valid,
                        yield_factors,
                    ),
                    f,
                )

    # compute batch weights that ensures that each class is equally represented in each batch
    # and that samples within a class are weighted according to their yield
    batch_weights: list[float] = []
    for label, _samples in labels_to_samples.items():
        if label == 0:
            # signal samples are to be drawn equally often
            batch_weights += [1 / len(_samples)] * len(_samples)
        else:
            # repeat backgrounds according to their yield in that class
            sum_yield_factors = sum(yield_factors[sample] for sample in _samples)
            for sample in _samples:
                batch_weights.append(yield_factors[sample] / sum_yield_factors)

    # compute weights to be applied to validation events to resemble the batch composition seen during training
    n_events_valid = list(map(len, event_weights_valid))
    sum_events_valid = sum(n_events_valid)
    sum_batch_weights = sum(batch_weights)
    composition_weights_valid: list[float] = [
        batch_weight / len(event_weights) * sum_events_valid / sum_batch_weights
        for batch_weight, event_weights in zip(batch_weights, event_weights_valid)
    ]
    # multiply to the original weights
    for i in range(len(event_weights_valid)):
        event_weights_valid[i] = event_weights_valid[i] * composition_weights_valid[i]

    # count number of training and validation events per class
    events_per_class = {
        label: (
            int(sum(sum(labels[:, label]) for labels in labels_train)),
            int(sum(sum(labels[:, label]) for labels in labels_valid)),
        )
        for label in unique_labels
    }

    # determine contiuous input means and variances
    cont_input_means = (
        np.sum(np.concatenate([inp * bw / len(inp) for inp, bw in zip(cont_inputs_train, batch_weights)]), axis=0) /
        sum(batch_weights)
    )
    cont_input_vars = (
        np.sum(np.concatenate([inp**2 * bw / len(inp) for inp, bw in zip(cont_inputs_train, batch_weights)]), axis=0) /
        sum(batch_weights)
    ) - cont_input_means**2

    # handle year
    add_year = False
    if parameterize_year:
        cat_input_names.append("year")
        add_year = True
    if regression_cfg and regression_cfg.parameterize_year:
        reg_cat_input_names.append("year")
        add_year = True
    if add_year:
        combined_cat_input_names.append("year")
        # add to possible embedding values
        possible_cat_input_values.append(embedding_expected_inputs["year"])

    # handle masses
    masses = sorted(float(sample.mass) for sample in samples if sample.mass >= 0)
    add_mass = False
    if parameterize_mass:
        cont_input_names.append("mass")
        add_mass = True
    if regression_cfg and regression_cfg.parameterize_mass:
        reg_cont_input_names.append("mass")
        add_mass = True
    if add_mass:
        assert len(masses) > 0
        combined_cont_input_names.append("mass")
        # replace mean and var with unweighted values
        cont_input_means[-1] = np.mean(masses)
        cont_input_vars[-1] = np.var(masses)

    # handle spins
    spins = sorted(int(sample.spin) for sample in samples if sample.spin >= 0)
    add_spin = False
    if parameterize_spin:
        cat_input_names.append("spin")
        add_spin = True
    if regression_cfg and regression_cfg.parameterize_spin:
        reg_cat_input_names.append("spin")
        add_spin = True
    if add_spin:
        assert len(spins) > 0
        combined_cat_input_names.append("spin")
        # add to possible embedding values
        possible_cat_input_values.append(embedding_expected_inputs["spin"])

    with device:
        # live transformation of inputs to inject spin and mass for backgrounds
        def transform(inst, cont_inputs, cat_inputs, labels, weights):
            if add_mass:
                neg_mass = cont_inputs[:, -1] < 0
                cont_inputs[:, -1][neg_mass] = np.random.choice(masses, size=neg_mass.sum())
            if add_spin:
                neg_spin = cat_inputs[:, -1] < 0
                cat_inputs[:, -1][neg_spin] = np.random.choice(spins, size=neg_spin.sum())
            return cont_inputs, cat_inputs, labels, weights

        # build datasets
        dataset_train = MultiDataset(
            data=zip(zip(cont_inputs_train, cat_inputs_train, labels_train, event_weights_train), batch_weights),
            batch_size=batch_size,
            kind="train",
            transform_data=transform,
            seed=seed,
        )
        dataset_valid = MultiDataset(
            data=zip(zip(cont_inputs_valid, cat_inputs_valid, labels_valid, event_weights_valid), batch_weights),
            kind="valid",
            yield_valid_rest=True,
            transform_data=transform,
            seed=seed,
        )

        # store tensors for selecting either features for the classification or regression-pre-NN
        regression_data = None
        if regression_cfg:
            def get_indices(combined_names, names):
                return tf.constant(list(map(combined_names.index, names)), dtype=tf.int32)
            regression_data = {
                "model_file": regression_cfg.model_files[fold_index],
                "use_last_layers": regression_cfg.use_last_layers,
                "cont_input_indices": get_indices(combined_cont_input_names, cont_input_names),
                "cat_input_indices": get_indices(combined_cat_input_names, cat_input_names),
                "reg_cont_input_indices": get_indices(combined_cont_input_names, reg_cont_input_names),
                "reg_cat_input_indices": get_indices(combined_cat_input_names, reg_cat_input_names),
            }

        # create the model
        model = create_model(
            n_cont_inputs=len(combined_cont_input_names),
            n_cat_inputs=len(combined_cat_input_names),
            regression_data=regression_data,
            n_classes=n_classes,
            embedding_expected_inputs=possible_cat_input_values,
            embedding_output_dim=embedding_output_dim,
            cont_input_means=cont_input_means,
            cont_input_vars=cont_input_vars,
            units=units,
            connection_type=connection_type,
            activation=activation,
            batch_norm=batch_norm,
            l2_norm=l2_norm,
            dropout_rate=dropout_rate,
        )
        if regression_cfg:
            reg_models = [layer for layer in model.layers if layer.name == "htautau_regression"]
            assert len(reg_models) == 1
            reg_model = reg_models[0]  # noqa

        # compile
        opt_cls = {
            "adam": tf.keras.optimizers.Adam,
            "adamw": tf.keras.optimizers.AdamW,
        }[optimizer]
        model.compile(
            loss="categorical_crossentropy",
            optimizer=opt_cls(
                learning_rate=learning_rate,
                jit_compile=jit_compile,
            ),
            weighted_metrics=[
                tf.keras.metrics.CategoricalCrossentropy(name="ce"),
                tf.keras.metrics.CategoricalAccuracy(name="acc"),
            ],
            metrics=[
                L2Metric(
                    model,
                    select_layers=(lambda model: model.l2_layers["main"]),
                    name="l2",
                ),
            ],
            jit_compile=jit_compile,
            run_eagerly=eager_mode,
        )

        # callback to repeat the lr and es scheduler once to enable fine-tuning of the regression pre-nn if set
        # see https://keras.io/guides/transfer_learning/#finetuning
        lres_repeat = None
        if regression_cfg and regression_cfg.fine_tune:
            def lres_repeat(lres_callback: ReduceLRAndStopAndRepeat, logs: dict[str, Any]) -> bool:  # noqa
                # only repeat once
                if lres_callback.repeat_counter != 0:
                    return False
                # 1. make the reg_model trainable
                reg_model.trainable = True
                # 2. update l2 norms (enabled those on reg_model and just update others)
                if l2_norm > 0:
                    # TODO: one could also consider different methods for resetting l2:
                    # a) use current weights of both networks to determine the new l2 norm such that the loss will be
                    #    equal to the current one of only the main NN
                    # b) like a), but set the new norm to 2x (3x, ...) the current one
                    # c) use the same norm, although this might lead to a way too large l2 component as the reg_model
                    #    usually has many weights
                    # some naive first shot:
                    n_weights_total = sum(map(
                        tf.keras.backend.count_params,
                        [layer.kernel for layer in model.l2_layers["main"] + model.l2_layers["reg"]],
                    ))
                    for layer in model.l2_layers["main"] + model.l2_layers["reg"]:
                        layer.kernel_regularizer.l2[...] = l2_norm / n_weights_total
                # 3. re-compile, optionally update learning rate
                model.compile(
                    loss="categorical_crossentropy",
                    optimizer=opt_cls(
                        learning_rate=tf.keras.backend.get_value(model.optimizer.lr),
                        jit_compile=jit_compile,
                    ),
                    weighted_metrics=[
                        tf.keras.metrics.CategoricalCrossentropy(name="ce"),
                        tf.keras.metrics.CategoricalAccuracy(name="acc"),
                    ],
                    metrics=[
                        L2Metric(
                            model,
                            select_layers=(lambda model: model.l2_layers["main"] + model.l2_layers["reg"]),
                            name="l2",
                        ),
                    ],
                    jit_compile=jit_compile,
                    run_eagerly=eager_mode,
                )
                # opt.iterations.assign(opt1.iterations)
                # 4. optionally update lr and es patiences, lr factor and reductions
                pass  # to be seen
                print(f"\nenabled fine-tuning of {reg_model.name} layers")
                return True

        # prepare the tensorboard dir
        full_tensorboard_dir = None
        if tensorboard_dir:
            full_tensorboard_dir = os.path.join(tensorboard_dir, model_name)
            if tensorboard_version:
                full_tensorboard_dir = os.path.join(full_tensorboard_dir, tensorboard_version)
            if clear_existing_tensorboard and os.path.exists(full_tensorboard_dir):
                shutil.rmtree(full_tensorboard_dir)

        # callbacks
        fit_callbacks = [
            # learning rate dropping followed by early stopping, optionally followed by enabling fine-tuning
            lres_callback := ReduceLRAndStopAndRepeat(
                monitor="val_ce",
                mode="min",
                lr_patience=learning_rate_patience,
                lr_factor=0.5,
                lr_reductions=learning_rate_reductions,
                es_patience=early_stopping_patience,
                repeat_func=lres_repeat,
                verbose=1,
            ),
            # tensorboard
            tf.keras.callbacks.TensorBoard(
                log_dir=full_tensorboard_dir,
                histogram_freq=1,
                write_graph=True,
                profile_batch=(500, 1500) if run_profiler else 0,
            ) if full_tensorboard_dir else None,
            # confusion matrix and output plots
            LivePlotWriter(
                log_dir=full_tensorboard_dir,
                class_names=list(class_names.values()),
                validate_every=validate_every,
            ) if full_tensorboard_dir else None,
        ]

        # some logs
        model.summary()
        header = ["Class (label)", "Total", "train", "valid"]
        rows = []
        for (label, (n_train, n_valid)), class_name in zip(events_per_class.items(), class_names.values()):
            rows.append([f"{class_name} ({label})", n_train + n_valid, n_train, n_valid])
        rows.append(["Total", len(dataset_train) + len(dataset_valid), len(dataset_train), len(dataset_valid)])
        print("")
        print(tabulate(rows, headers=header, tablefmt="github", intfmt="_"))
        print("")

        # training
        t_start = time.perf_counter()
        try:
            model.fit(
                x=dataset_train.create_keras_generator(input_names=["cont_input", "cat_input"]),
                validation_data=dataset_valid.create_keras_generator(input_names=["cont_input", "cat_input"]),
                shuffle=False,  # the custom generators already shuffle
                epochs=max_epochs,
                steps_per_epoch=validate_every,
                validation_freq=1,
                validation_steps=1,
                callbacks=list(filter(None, fit_callbacks)),
            )
            # model.load_weights("/gpfs/dust/cms/user/riegerma/taunn_data/store/Training/dev_weights/hbtres_LSbinary_FSreg-reg_ED5_LU5x128_CTfcn_ACTelu_BNy_LT50_DO0_BS4096_LR3.0e-03_SPINy_MASSy_FI0_SD1")  # noqa

            t_end = time.perf_counter()
        except KeyboardInterrupt:
            t_end = time.perf_counter()
            print("\n\ndetected manual interrupt!\n")
            print("type 's' to gracefully stop training and save the model,")
            try:
                inp = input("or any other key to terminate directly without saving: ")
            except KeyboardInterrupt:
                inp = ""
            if inp != "s":
                print("model not saved")
                return
            print("")
            # manually restore best weights
            lres_callback.restore_best_weights()
        print(f"training took {human_duration(seconds=t_end - t_start)}")

        # perform one final validation round for verification of the best model
        print("performing final round of validation")
        results_valid = model.evaluate(
            x=dataset_valid.create_keras_generator(input_names=["cont_input", "cat_input"]),
            steps=1,
            return_dict=True,
        )

        # model saving
        def save_model(path):
            print(f"saving model at {path}")
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))

            # save the model using tf's savedmodel format
            tf.keras.saving.save_model(
                model,
                path,
                overwrite=True,
                save_format="tf",
                include_optimizer=False,
            )

            # and in the new .keras high-level format
            keras_path = os.path.join(path, "model.keras")
            if os.path.exists(keras_path):
                os.remove(keras_path)
            model.save(
                keras_path,
                overwrite=True,
                save_format="keras",
            )

            # save an accompanying json file with hyper-parameters, input names and other info
            meta = {
                "model_name": model_name,
                "sample_names": [sample.name for sample in samples],
                "class_names": class_names,
                "input_names": {
                    "cont": combined_cont_input_names,
                    "cat": combined_cat_input_names,
                },
                "n_classes": n_classes,
                "n_folds": n_folds,
                "fold_index": fold_index,
                "validation_fraction": validation_fraction,
                "seed": seed,
                "architecture": {
                    "units": units,
                    "embedding_output_dim": embedding_output_dim,
                    "activation": activation,
                    "connection_type": connection_type,
                    "l2_norm": l2_norm,
                    "drop_out": dropout_rate,
                    "batch_norm": batch_norm,
                    "batch_size": batch_size,
                    "learning_rate": learning_rate,
                    "optimizer": optimizer,
                    "final_learning_rate": float(model.optimizer.lr.numpy()),
                    "parameterize_spin": parameterize_spin,
                    "parameterize_mass": parameterize_mass,
                    "regression_set": regression_set,
                },
                "result": {
                    **results_valid,
                    "steps_trained": int(model.optimizer.iterations.numpy()),
                },
            }
            if regression_cfg:
                meta["input_names"]["reg_cont"] = reg_cont_input_names
                meta["input_names"]["reg_cat"] = reg_cat_input_names
            with open(os.path.join(path, "meta.json"), "w") as f:
                json.dump(meta, f, indent=4)

            return path

        # save at actual location, fallback to tmp dir
        try:
            model_path = save_model(os.path.join(model_dir, model_name))
        except (OSError, ValueError) as e:
            if not model_fallback_dir:
                raise e
            print(f"saving at default path failed: {e}")
            model_path = save_model(os.path.join(model_fallback_dir, model_name))

    return model, model_path


# functional model builder for later use with hyperparameter optimization tools
# via https://www.tensorflow.org/tutorials/keras/keras_tuner
def create_model(
    *,
    n_cont_inputs: int,
    n_cat_inputs: int,
    regression_data: dict[str, Any] | None,
    n_classes: int,
    embedding_expected_inputs: list[list[int]],
    embedding_output_dim: int,
    cont_input_means: np.ndarray,
    cont_input_vars: np.ndarray,
    units: list[int],
    connection_type: str,
    activation: str,
    batch_norm: bool,
    l2_norm: float,
    dropout_rate: float,
):
    """
    ResNet: https://arxiv.org/pdf/1512.03385.pdf
    DenseNet: https://arxiv.org/pdf/1608.06993.pdf
    """
    # checks
    assert len(embedding_expected_inputs) == n_cat_inputs
    assert len(cont_input_means) == len(cont_input_vars) == n_cont_inputs
    assert connection_type in ["fcn", "res", "dense"]
    assert len(units) > 0
    assert regression_data is None or len(regression_data) == 6

    # get activation settings
    act_settings = activation_settings[activation]

    # input layers
    x_cont = tf.keras.Input(n_cont_inputs, dtype=tf.float32, name="cont_input")
    x_cat = tf.keras.Input(n_cat_inputs, dtype=tf.int32, name="cat_input")

    # layers that define the total input in the DNN that are concatenated
    dnn_input_layers = []

    # regression pre-NN
    if regression_data:
        # load the model
        reg_model = tf.keras.models.load_model(regression_data["model_file"])

        # add back empty kernel regualizers to all dense layers
        for layer in reg_model.layers:
            if isinstance(layer, tf.keras.layers.Dense):
                layer.kernel_regularizer = tf.keras.regularizers.l2(0.0) if l2_norm > 0 else None

        # make layers non-trainable at first
        reg_model.trainable = False

        # get the pre-NN inputs
        reg_cont = tf.gather(x_cont, regression_data["reg_cont_input_indices"], axis=1, name="select_reg_cont_inputs")
        reg_cat = tf.gather(x_cat, regression_data["reg_cat_input_indices"], axis=1, name="select_reg_cat_inputs")

        # run the pre-NN
        reg_out_reg, _, reg_out_cls, reg_out_reg_last, reg_out_cls_last = reg_model([reg_cont, reg_cat])

        # concatenate all regression outputs
        reg_concat_layers = [reg_out_reg, reg_out_cls]
        if regression_data["use_last_layers"]:
            reg_concat_layers += [reg_out_reg_last, reg_out_cls_last]
        reg_out = tf.keras.layers.Concatenate(name="reg_concat")(reg_concat_layers)

        # batch normalize the regression outputs
        reg_out = tf.keras.layers.BatchNormalization(name="reg_batchnorm")(reg_out)

        # define as input
        dnn_input_layers.append(reg_out)

        # update original inputs
        dnn_cont = tf.gather(x_cont, regression_data["cont_input_indices"], axis=1, name="select_dnn_cont_inputs")
        dnn_cat = tf.gather(x_cat, regression_data["cat_input_indices"], axis=1, name="select_dnn_cat_inputs")

    else:
        # no regression as pre-nn, use all inputs as dnn inputs
        dnn_cont = x_cont
        dnn_cat = x_cat

    # embedding layer
    if n_cat_inputs > 0:
        # encode categorical inputs to indices
        dnn_cat_encoded = EmbeddingEncoder(embedding_expected_inputs, name="cat_encoder")(dnn_cat)

        # actual embedding
        dnn_cat_embedded = tf.keras.layers.Embedding(
            input_dim=sum(map(len, embedding_expected_inputs)),
            output_dim=embedding_output_dim,
            input_length=n_cat_inputs,
            name="cat_embedded",
        )(dnn_cat_encoded)

        # flatten
        dnn_cat_embedded_flat = tf.keras.layers.Flatten(name="cat_flat")(dnn_cat_embedded)

        # define as input
        dnn_input_layers.insert(0, dnn_cat_embedded_flat)

    # normalize continuous inputs and define as input
    dnn_cont_norm = tf.keras.layers.Normalization(
        mean=cont_input_means,
        variance=cont_input_vars,
        name="dnn_input_norm",
    )(dnn_cont)
    dnn_input_layers.insert(0, dnn_cont_norm)

    # concatenate all inputs
    shape_elems = (f"{layer.name}({i})->{layer.shape[1]}" for i, layer in enumerate(dnn_input_layers))
    print(f"using {len(dnn_input_layers)} DNN inputs with shapes {', '.join(shape_elems)}")
    a = tf.keras.layers.Concatenate(name="input_concat")(dnn_input_layers)

    # previous resnet layer for pairwise addition
    res_prev: tf.keras.layers.Layer | None = None

    # previous dense layer for concatenation
    dense_prev: tf.keras.layers.Layer | None = None

    # add layers programatically
    for i, n_units in enumerate(units, 1):
        # dense
        dense_layer = tf.keras.layers.Dense(
            n_units,
            use_bias=True,
            kernel_initializer=act_settings.weight_init,
            kernel_regularizer=tf.keras.regularizers.l2(0.0) if l2_norm > 0 else None,
            name=f"dense_{i}")
        a = dense_layer(a)

        # batch norm before activation if requested
        batchnorm_layer = tf.keras.layers.BatchNormalization(dtype=tf.float32, name=f"batchnorm_{i}")
        batch_norm_before, batch_norm_after = act_settings.batch_norm
        if batch_norm and batch_norm_before:
            a = batchnorm_layer(a)

        # add with previous resnet layer on next even layer
        if connection_type == "res" and i % 2 == 0 and res_prev is not None:
            a = tf.keras.layers.Add(name=f"res_add_{i}")([a, res_prev])

        # activation
        a = tf.keras.layers.Activation(act_settings.name, name=f"act_{i}")(a)

        # batch norm after activation if requested
        if batch_norm and batch_norm_after:
            a = batchnorm_layer(a)

        # add random unit dropout
        if dropout_rate:
            dropout_cls = getattr(tf.keras.layers, act_settings.dropout_name)
            a = dropout_cls(dropout_rate, name=f"do_{i}")(a)

        # save for resnet
        if connection_type == "res" and i % 2 == 0:
            res_prev = a

        # concatenate with previous dense layer to define new output
        if connection_type == "dense":
            if dense_prev is not None:
                a = tf.keras.layers.Concatenate(name=f"dense_concat_{i}")([a, dense_prev])
            dense_prev = a

    # add the output layer
    a = tf.keras.layers.Dense(
        n_classes,
        use_bias=True,
        kernel_initializer=activation_settings["softmax"].weight_init,
        kernel_regularizer=tf.keras.regularizers.l2(0.0) if l2_norm > 0 else None,
        name=f"dense_{i + 1}",
    )(a)
    y = tf.keras.layers.Activation("softmax", name="output")(a)

    # build the model
    log_live_plots = False
    model_cls = ClassificationModelWithValidationBuffers if log_live_plots else tf.keras.Model
    model = model_cls(inputs=[x_cont, x_cat], outputs=[y], name="bbtautau_classifier")

    # lookup layers whose kernels should be subject to l2
    l2_layers = {
        "main": [
            layer for layer in model.layers
            if isinstance(layer, tf.keras.layers.Dense) and layer.kernel_regularizer is not None
        ],
    }
    if regression_data:
        l2_layers["reg"] = [
            layer for layer in reg_model.layers
            if isinstance(layer, tf.keras.layers.Dense) and layer.kernel_regularizer is not None
        ]
    # add them as attributes to the model which enables keras to track them to compute the overall l2 loss
    # (note: they will be listed in model.layers as well, and main l2 layers are not counted twice)
    model.l2_layers = l2_layers

    # scale the l2 regularization to the number of weights in dense layers of the main network
    # (the pre-nn is not included yet as it is not trainable at first by default, so once fine-tuning is enabled,
    # the l2 regularization should be updated accordingly)
    if l2_norm > 0:
        # compute number of weights in the main network
        n_weights_main = sum(map(
            tf.keras.backend.count_params,
            [layer.kernel for layer in l2_layers["main"]],
        ))
        # compute the scaled l2 norm
        l2_norm_scaled = l2_norm / n_weights_main
        print(f"scaled l2 norm from {l2_norm:.1f} to {l2_norm_scaled:5f} based in {n_weights_main} weights")
        # update regularizers
        for layer in l2_layers["main"]:
            layer.kernel_regularizer.l2[...] = l2_norm_scaled

    return model


def main() -> None:
    train()


if __name__ == "__main__":
    main()
