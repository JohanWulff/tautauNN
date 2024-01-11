# coding: utf-8

from __future__ import annotations

import os
import re
import math
import glob
import time
import hashlib
import pickle
import inspect
import fnmatch
import itertools
from multiprocessing import Pool
from typing import Any

import numpy as np
import numpy.lib.recfunctions as rfn
import uproot
import awkward as ak
import vector
import tensorflow as tf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from law.util import human_duration


epsilon = 1e-6


def _load_root_file_impl(file_name: str, features: list[str], selections: str) -> tuple[np.recarray, float, str] | str:
    from tautaunn.config import klub_aliases

    with uproot.open(file_name) as f:
        if "HTauTauTree" not in f or "h_eff" not in f:
            return file_name
        tree = f["HTauTauTree"]
        ak_array = tree.arrays(features, cut=selections, aliases=klub_aliases, library="ak")
        ak_array = ak.with_field(ak_array, 1.0, "sum_weights")
        rec = ak_array.to_numpy()
        return rec, f["h_eff"].values()[0], file_name


def _load_root_file_impl_mp(args):
    return _load_root_file_impl(*args)


def load_sample_root(data_dir, sample, features, selections, max_events=-1, cache_dir=None, n_threads=4):
    print(f"loading sample {sample.skim_name} ... ", end="", flush=True)

    # potentially read from cache
    cache_path = get_cache_path(cache_dir, data_dir, sample, features, selections, max_events)
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            feature_vecs = pickle.load(f)
        print(f"loaded {len(feature_vecs):_} events from cache")

    else:
        feature_vecs = []
        n_events = 0
        sum_weights = 0.0
        broken_files = []
        file_names = glob.glob(f"{data_dir}/{sample.directory_name}/output_*.root")

        # load files in parallel
        n_files_seen = len(file_names)
        pool_args = [(file_name, features, selections) for file_name in file_names]
        t0 = time.perf_counter()
        with Pool(n_threads) as pool:
            for result in pool.imap(_load_root_file_impl_mp, pool_args):
                if isinstance(result, str):
                    broken_files.append(result)
                    continue
                rec, file_sum_weights, _ = result
                feature_vecs.append(rec)
                n_events += len(rec)
                sum_weights += file_sum_weights
                if max_events > 0 and n_events > max_events:
                    break
        duration = time.perf_counter() - t0

        # concatenate and add sum_weights column
        feature_vecs = np.concatenate(feature_vecs, axis=0)
        feature_vecs["sum_weights"] *= sum_weights
        print(f"loaded {len(feature_vecs):_} events from {n_files_seen} file(s), took {human_duration(seconds=duration)}")
        if broken_files:
            broken_files_repr = "\n".join(broken_files)
            print(f"{len(broken_files)} broken file(s):\n{broken_files_repr}")

        # save to cache
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(feature_vecs, f)

    return feature_vecs


def transform_data_dir_cache(data_dir: str) -> str:
    """
    Function to transform the data directory to be used as a fragment to determine the cache path.
    """
    # consider consider gpfs paths on maxwell as nfs paths (since the disks are identical but mount points differ)
    if data_dir.startswith("/gpfs/"):
        data_dir = f"/nfs/{data_dir[6:]}"
    return data_dir


def get_cache_path(cache_dir, data_dir, sample, features, selections, maxevents) -> str | None:
    if not cache_dir:
        return None

    cache_key = [
        transform_data_dir_cache(os.path.expandvars(data_dir)),
        sample.skim_name,
        sorted(features),
        (
            selections.replace(" ", "")
            if isinstance(selections, str)
            else [(feats, inspect.getsource(func)) for feats, func in selections]
        ),
        maxevents,
    ]
    cache_hash = hashlib.sha256(str(cache_key).encode("utf-8")).hexdigest()[:10]
    return os.path.join(cache_dir, f"{sample.skim_name}_{cache_hash}.pkl")


def match(value: str, pattern: str) -> bool:
    if pattern.startswith("^") and pattern.endswith("$"):
        return bool(re.match(pattern, value))
    return bool(fnmatch.fnmatch(value, pattern))


def calc_new_columns(data, rules):
    columns = []
    column_names = []
    for name, (input_columns, func) in rules.items():
        if name in data.dtype.names:
            continue
        input_values = [columns[column_names.index(c)] if c in column_names else data[c] for c in input_columns]
        column = func(*input_values)
        columns.append(column)
        column_names.append(name)
    data = rfn.rec_append_fields(data, list(rules.keys()), columns, dtypes=["<f4"] * len(columns))
    return data


def calc_4vec_sum(pt1, eta1, phi1, e1, pt2, eta2, phi2, e2):
    px1 = pt1 * np.cos(phi1)
    py1 = pt1 * np.sin(phi1)
    pz1 = pt1 * np.sinh(eta1)

    px2 = pt2 * np.cos(phi2)
    py2 = pt2 * np.sin(phi2)
    pz2 = pt2 * np.sinh(eta2)

    px = px1 + px2
    py = py1 + py2
    pz = pz1 + pz2
    e = e1 + e2

    pt = np.sqrt(px**2 + py**2)
    p = np.sqrt(pt**2 + pz**2)
    theta = np.arccos(pz / p)
    eta = -np.log(np.tan(theta / 2))
    phi = np.arccos(px / pt)
    phi[py == 0] = 0
    phi[py < 0] = -np.arccos(px / pt)[py < 0]

    return pt, eta, phi, e


def calc_energy(pt, eta, phi, m):
    px = pt * np.cos(phi)
    py = pt * np.sin(phi)
    pz = pt * np.sinh(eta)

    energy = np.sqrt(m**2 + px**2 + py**2 + pz**2)

    return energy


def calc_mass(pt, eta, phi, e):
    px = pt * np.cos(phi)
    py = pt * np.sin(phi)
    pz = pt * np.sinh(eta)

    mass = np.sqrt(e**2 - px**2 - py**2 - pz**2)
    mass[np.isnan(mass)] = 0

    return mass


def phi_mpi_to_pi(phi):
    larger_pi = phi > math.pi
    smaller_pi = phi < -math.pi
    while np.any(larger_pi) or np.any(smaller_pi):
        phi[larger_pi] -= 2 * math.pi
        phi[smaller_pi] += 2 * math.pi
        larger_pi = phi > math.pi
        smaller_pi = phi < -math.pi
    return phi


def calc_top_masses(l_1, l_2, b_1, b_2, met):
    # build all possible combinations of l,b and met
    vector_mass_top = np.array([
        ((l_1 + b_1 + met).mass, (l_2 + b_2).mass),
        ((l_1 + b_2 + met).mass, (l_2 + b_1).mass),
        ((l_1 + b_1).mass, (l_2 + b_2 + met).mass),
        ((l_1 + b_2).mass, (l_2 + b_1 + met).mass),
    ])
    # calculate distance to top mass
    distance = np.array([(mass[0] - 172.5) ** 2 + (mass[1] - 172.5) ** 2 for mass in vector_mass_top])
    # get index (0-3) of object comb. which was closest
    min_dis = np.argmin(distance, axis=0)
    # get the corresponding object comb. from vector_mass_top
    top_masses = [(vector_mass_top[m][0][i], vector_mass_top[m][1][i]) for i, m in enumerate(min_dis)]
    # sort them such that the one with the largest mass is always first
    top_masses = [sorted(m, reverse=True) for m in top_masses]
    return top_masses, min_dis


def top_info(
    kind: str,
    dau1_pt, dau1_eta, dau1_phi, dau1_e,
    dau2_pt, dau2_eta, dau2_phi, dau2_e,
    bjet1_pt, bjet1_eta, bjet1_phi, bjet1_e,
    bjet2_pt, bjet2_eta, bjet2_phi, bjet2_e,
    met_et, met_phi,
):
    dau1 = vector.array({"pt": dau1_pt, "eta": dau1_eta, "phi": dau1_phi, "e": dau1_e})
    dau2 = vector.array({"pt": dau2_pt, "eta": dau2_eta, "phi": dau2_phi, "e": dau2_e})
    bjet1 = vector.array({"pt": bjet1_pt, "eta": bjet1_eta, "phi": bjet1_phi, "e": bjet1_e})
    bjet2 = vector.array({"pt": bjet2_pt, "eta": bjet2_eta, "phi": bjet2_phi, "e": bjet2_e})
    met = vector.array({"pt": met_et, "eta": np.zeros_like(dau1_pt), "phi": met_phi, "mass": np.zeros_like(dau1_pt)})
    top_masses, top_mass_idx = calc_top_masses(l_1=dau1, l_2=dau2, b_1=bjet1, b_2=bjet2, met=met)

    # return what is requested
    if kind == "top1_mass":
        return np.array([m[0] for m in top_masses], dtype=np.float32)
    if kind == "top2_mass":
        return np.array([m[1] for m in top_masses], dtype=np.float32)
    if kind == "indices":
        return np.asarray(top_mass_idx, dtype=np.int32)

    raise ValueError(f"unknown top_info kind {kind}")


def create_tensorboard_callbacks(log_dir):
    add = flush = lambda *args, **kwargs: None
    if log_dir:
        writer = tf.summary.create_file_writer(log_dir)
        flush = writer.flush

        def add(attr, *args, **kwargs):  # noqa
            with writer.as_default():
                getattr(tf.summary, attr)(*args, **kwargs)
    return add, flush


def create_model_name(*, model_name=None, model_prefix=None, model_suffix=None, **params):
    if model_name is None:
        name_parts = {}

        def add(key, name, fmt=None):
            if not callable(fmt):
                fmt = lambda x: x
            if name in params:
                assert key not in name_parts
                name_parts[key] = fmt(params.pop(name))

        add("ps", "selection_set")
        add("ls", "label_set")
        add("ss", "sample_set")
        add("fs", "feature_set")
        add("ed", "embedding_output_dim")
        add("lu", "units", lambda x: f"{len(x)}x{x[0]}")
        add("ct", "connection_type")
        add("act", "activation")
        add("bn", "batch_norm")
        add("lt", "l2_norm")
        add("do", "dropout_rate")
        add("bs", "batch_size")
        add("op", "optimizer")
        add("lr", "learning_rate")
        add("year", "parameterize_year")
        add("spin", "parameterize_spin")
        add("mass", "parameterize_mass")
        add("rs", "regression_set")
        add("bw", "background_weight")
        add("fi", "fold_index")
        add("fi", "fold_indices")
        add("sd", "seed")
        add("sd", "seeds")

        if params:
            raise ValueError(f"unhandled hyper-parameters for creating model model: {params}")

        model_name = "_".join(f"{k.upper()}{encode_hyper_param(v)}" for k, v in name_parts.items())

    if model_prefix:
        model_name = f"{model_prefix.rstrip('_')}_{model_name}"

    if model_suffix:
        model_name = f"{model_name}_{model_suffix.lstrip('_')}"

    return model_name


def encode_hyper_param(value: Any) -> str:
    # conversions
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    # encodings
    if value is None:
        return "none"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "ny"[value]
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if 0.01 <= abs(value) <= 100.0 else f"{value:.1e}"
    if isinstance(value, (list, tuple)):
        return "_".join(map(encode_hyper_param, value))
    raise NotImplementedError(f"cannot encode hyper parameter '{value}'")


def plot_confusion_matrix(cm: np.ndarray, class_names: list[str], colorbar: bool = True):
    fig, ax = plt.subplots()

    # draw matrix and colorbar
    im = ax.imshow(cm, interpolation="nearest", cmap=plt.cm.Blues)
    if colorbar:
        fig.colorbar(im)

    # styles
    ax.set_title("Confusion matrix")
    tick_marks = np.arange(len(class_names))
    ax.set_xticks(tick_marks, class_names, rotation=45)
    ax.set_yticks(tick_marks, class_names)
    ax.set_xlabel("Predicted class")
    ax.set_ylabel("True class")

    # cell labels
    labels = np.around(cm.astype("float") / cm.sum(axis=1)[:, None], decimals=2)
    white_threshold = 0.5 * cm.max()
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        color = "white" if cm[i, j] > white_threshold else "black"
        ax.text(j, i, labels[i, j], horizontalalignment="center", color=color)

    fig.tight_layout()

    return fig, ax


def plot_class_outputs(
    predictions: np.ndarray,
    truth: np.ndarray,
    class_index: int,
    class_names: list[str],
    n_bins: int = 50,
):
    fig, ax = plt.subplots()

    # plot histograms
    bins = np.linspace(0, 1, 21)
    for i, name in enumerate(class_names):
        values = predictions[:, class_index][truth[:, i] == 1]
        ax.hist(values, bins, alpha=0.5, label=name, density=True)
    ax.legend()

    # styles
    ax.set_title(f"Node '{class_names[class_index]}' output")
    ax.set_xlabel(f"DNN output {class_names[class_index]}")
    ax.set_ylabel("Normalized events")

    fig.tight_layout()

    return fig, ax
