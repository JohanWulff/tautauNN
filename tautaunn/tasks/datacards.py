# coding: utf-8

from __future__ import annotations

import os
import re
import itertools
from collections import defaultdict

import luigi
import law
import numpy as np
import tensorflow as tf
import awkward as ak

from tautaunn.tasks.base import SkimWorkflow, MultiSkimTask
from tautaunn.tasks.training import MultiFoldParameters, ExportEnsemble
from tautaunn.util import calc_new_columns
from tautaunn.tf_util import get_device
import tautaunn.config as cfg


class EvaluationParameters(MultiFoldParameters):

    spins = law.CSVParameter(
        cls=luigi.IntParameter,
        default=tuple(cfg.spins),
        description=f"spins to evaluate; default: {','.join(map(str, cfg.spins))}",
        brace_expand=True,
    )
    masses = law.CSVParameter(
        cls=luigi.FloatParameter,
        default=tuple(cfg.masses),
        description=f"masses to evaluate; default: {','.join(map(str, cfg.masses))}",
        brace_expand=True,
    )


class EvaluateSkims(SkimWorkflow, EvaluationParameters):

    @property
    def chunk_size(self):
        return 1
        if re.match("^.*(TT_semiLep|TT_fullyLep|ttHToTauTau|TTZToQQ|201).*$", self.sample.name):
            return 1
        return 2

    @property
    def priority(self):
        # higher priority value = picked earlier by scheduler
        if re.match("^.*(TT_semiLep|TT_fullyLep|ttHToTauTau|TTZToQQ).*$", self.sample.name):
            return 10
        return 0

    def workflow_requires(self):
        reqs = super().workflow_requires()
        reqs["ensembles"] = {i: ExportEnsemble.req(self, fold=i) for i in self.flat_folds}
        return reqs

    def requires(self):
        return {i: ExportEnsemble.req(self, fold=i) for i in self.flat_folds}

    def store_parts(self):
        parts = super().store_parts()
        parts.insert_before("version", "ensemble", self.get_model_name())
        return parts

    def output(self):
        #return law.SiblingFileCollection({
            #num: self.local_target(f"output_{num}.root")
            #for num in self.branch_data
        #})
        return law.SiblingFileCollection({
            num: {f"nominal_{num}": self.local_target(f"output_{num}_nominal.root"),
                  f"shapes_{num}": self.local_target(f"output_{num}_shapes.root")}
            for num in self.branch_data
        })

    @law.decorator.safe_output
    def run(self):
        # set inter and intra op parallelism threads of tensorflow
        tf.config.threading.set_inter_op_parallelism_threads(1)
        tf.config.threading.set_intra_op_parallelism_threads(1)

        # helpers
        flatten = lambda r, t: r.astype([(n, t) for n in r.dtype.names], copy=False).view(t).reshape((-1, len(r.dtype)))
        def col_name(mass, spin, class_name, shape_name="nominal"):
            name = f"hbtresdnn_mass{int(mass)}_spin{int(spin)}_{class_name.lower()}"
            if shape_name != "nominal":
                name += f"_{shape_name}"
            return name

        def calc_inputs(arr, dyn_names, cfg, fold_index, models):
            arr = calc_new_columns(arr, {name: cfg.dynamic_columns[name] for name in dyn_names})
            # prepare model inputs
            cont_inputs = flatten(ak.to_numpy(arr[cont_input_names]), np.float32)
            cat_inputs = flatten(ak.to_numpy(arr[cat_input_names]), np.int32)

            # add year
            y = self.sample.year_flag
            cat_inputs = np.append(cat_inputs, y * np.ones(len(cat_inputs), dtype=np.int32)[..., None], axis=1)

            # reserve column for mass
            cont_inputs = np.append(cont_inputs, -1 * np.ones(len(cont_inputs), dtype=np.float32)[..., None], axis=1)

            # reserve column for spin (must be behind year!)
            cat_inputs = np.append(cat_inputs, -1 * np.ones(len(cat_inputs), dtype=np.int32)[..., None], axis=1)

            # create a mask to only select events whose categorical features were seen during training
            cat_mask = np.ones(len(arr), dtype=bool)
            for i, name in enumerate(cat_input_names):
                cat_mask &= np.isin(cat_inputs[:, i], np.unique(cfg.embedding_expected_inputs[name]))
            self.publish_message(f"events passing cat_mask: {cat_mask.mean() * 100:.2f}%")
            # merge with fold mask in case there are multiple models
            eval_mask = cat_mask
            if len(models) > 1:
                eval_mask &= (arr.EventNumber % self.n_folds) == fold_index
            return cont_inputs, cat_inputs, eval_mask

        def eval(model, cont_inputs, cat_inputs,
                 spins, masses, eval_mask, class_names,
                 shape_name, out_tree):
            for spin in spins:
                # insert spin
                cat_inputs[:, -1] = int(spin)
                for mass in masses:
                    # insert mass
                    cont_inputs[:, -1] = float(mass)

                    # evaluate
                    predictions = model([cont_inputs[eval_mask], cat_inputs[eval_mask]], training=False)

                    # insert into output tree
                    for i, class_name in enumerate(class_names.values()):
                        # HARDCODED: skip dy
                        # TODO: maybe also drop ttbar
                        if class_name == "dy":
                            continue
                        field = col_name(mass, spin, class_name, shape_name)
                        if field not in out_tree:
                            pred_arr = -1* np.ones(len(eval_mask), dtype=np.float32)
                            pred_arr[eval_mask] = predictions[:, i]
                        out_tree[field] = pred_arr
            return out_tree

        def sel_trigger(array: ak.Array) -> ak.Array:
            return ((array.isLeptrigger == 1) | (array.isMETtrigger == 1) | (array.isSingleTauTrigger == 1))

        def sel_btag_m(array: ak.Array, year: str) -> ak.Array:
            return (
                (array.bjet1_bID_deepFlavor > cfg.btag_wps[year]["medium"]) &
                (array.bjet2_bID_deepFlavor < cfg.btag_wps[year]["medium"])
            ) | (
                (array.bjet1_bID_deepFlavor < cfg.btag_wps[year]["medium"]) &
                (array.bjet2_bID_deepFlavor > cfg.btag_wps[year]["medium"])
            )

        def sel_btag_mm(array: ak.Array, year: str) -> ak.Array:
            return (
                (array.bjet1_bID_deepFlavor > cfg.btag_wps[year]["medium"]) &
                (array.bjet2_bID_deepFlavor > cfg.btag_wps[year]["medium"])
            )

        def sel_pnet_l(array: ak.Array, year: str) -> ak.Array:
            return (
                (array.fatjet_particleNetMDJetTags_score > cfg.pnet_wps[year]["loose"])
            )

        def sel_cats(array: ak.Array, year: str) -> ak.Array:
            return ((
                sel_trigger(array) &
                (array.nleps == 0) &
                (array.nbjetscand > 1) &
                sel_btag_m(array, year) &
                ~(array.isBoosted == 1) # res1b
            ) | (
                sel_trigger(array) &
                (array.nleps == 0) &
                (array.nbjetscand > 1) &
                sel_btag_mm(array, year) &
                ~(array.isBoosted == 1) # res2b
            ) | (
                sel_trigger(array) &
                (array.nleps == 0) &
                (array.isBoosted == 1) &
                sel_pnet_l(array, year) 
            ))

        # ees has 2 sources
        ees_dict = {
            f"ele{ud}_{dm}": {
                "dau1_pt": f"dau1_pt_ele{ud}_{dm}",
                "dau1_e": f"dau1_e_ele{ud}_{dm}",
                "dau2_pt": f"dau2_pt_ele{ud}_{dm}",
                "dau2_e": f"dau2_e_ele{ud}_{dm}",
                "METx": f"METx_ele{ud}_{dm}",
                "METy": f"METy_ele{ud}_{dm}",
        }
        for ud in ["up", "down"] for dm in ["DM0", "DM1"]
        }
        # tes has 4 sources
        tes_dict = {
            f"tau{ud}_{dm}": {
                "dau1_pt": f"dau1_pt_tau{ud}_{dm}",
                "dau1_e": f"dau1_e_tau{ud}_{dm}",
                "dau2_pt": f"dau2_pt_tau{ud}_{dm}",
                "dau2_e": f"dau2_e_tau{ud}_{dm}",
                "METx": f"METx_tau{ud}_{dm}",
                "METy": f"METy_tau{ud}_{dm}",
        }
        for ud in ["up", "down"] for dm in ["DM0", "DM1", "DM10", "DM11"]
        }
        # jes has 11 sources
        jes_dict = {
            f"jet{ud}_{src}": {
                "dau1_pt": f"dau1_pt_jet{ud}_{src}",
                "dau1_e": f"dau1_e_jet{ud}_{src}",
                "dau2_pt": f"dau2_pt_jet{ud}_{src}",
                "dau2_e": f"dau2_e_jet{ud}_{src}",
                "METx": f"METx_jet{ud}_{src}",
                "METy": f"METy_jet{ud}_{src}",
        }
        for ud in ["up", "down"] for src in range(1, 12)
        }
            
        # klub aliases for systematic variations
        shape_systs = {
            "nominal": {
                "dau1_pt": "dau1_pt",
                "dau1_e": "dau1_e",
                "dau2_pt": "dau2_pt",
                "dau2_e": "dau2_e",
                "METx": "METx",
                "METy": "METy",
        },
        **ees_dict,
        **tes_dict,
        **jes_dict
        }
        shape_names = list(shape_systs.keys())  # all by default, can be redruced to subset

        # determine columns to read
        columns_to_read = set()
        columns_to_read |= set(cfg.cont_feature_sets[self.cont_feature_set])
        columns_to_read |= set(cfg.cat_feature_sets[self.cat_feature_set])
        if self.regression_cfg is not None:
            columns_to_read |= set(cfg.cont_feature_sets[self.regression_cfg.cont_feature_set])
            columns_to_read |= set(cfg.cat_feature_sets[self.regression_cfg.cat_feature_set])
        if self.lbn_cfg is not None:
            columns_to_read |= set(self.lbn_cfg.input_features) - {None}
        columns_to_read |= set(cfg.klub_index_columns)
        columns_to_read |= set(cfg.klub_category_columns)
        # expand dynamic columns, keeping track of those that are needed
        all_dyn_names = set(cfg.dynamic_columns)
        dyn_names = set()
        while (to_expand := columns_to_read & all_dyn_names):
            for name in to_expand:
                columns_to_read |= set(cfg.dynamic_columns[name][0])
            columns_to_read -= to_expand
            dyn_names |= to_expand
        dyn_names = sorted(dyn_names, key=list(cfg.dynamic_columns.keys()).index)

        # test: extend columns_to_read with systematic variations
        for shape_name in shape_systs.keys():
            for src, dst in shape_systs[shape_name].items():
                if src in columns_to_read:
                    columns_to_read.add(dst)

        # determine names of inputs
        cont_input_names = list(cfg.cont_feature_sets[self.cont_feature_set])
        cat_input_names = list(cfg.cat_feature_sets[self.cat_feature_set])
        if self.regression_cfg is not None:
            for name in cfg.cont_feature_sets[self.regression_cfg.cont_feature_set]:
                if name not in cont_input_names:
                    cont_input_names.append(name)
            for name in cfg.cat_feature_sets[self.regression_cfg.cat_feature_set]:
                if name not in cat_input_names:
                    cat_input_names.append(name)
        if self.lbn_cfg is not None:
            for name in self.lbn_cfg.input_features:
                if name and name not in cont_input_names:
                    cont_input_names.append(name)

        # get class names
        class_names = {
            label: data["name"].lower()
            for label, data in cfg.label_sets[self.label_set].items()
        }

        # callback to report progress
        publish_progress = self.create_progress_callback(
            len(self.flat_folds) *
            len(self.branch_data) *
            len(shape_names) *
            len(self.spins) *
            len(self.masses),
        )
        progress_step = 0

        # prepare input models
        models = dict(self.input().items())
        assert len(models) == 1 or set(models.keys()) == set(range(self.n_folds))

        # prepare outputs that will first be created in a temporary location and then moved eventually
        output_collection = self.output()
        tmp_outputs = {
            num: {output: law.LocalFileTarget(is_tmp="root")
                  for output in output_dict.values() if not output.exists()}
            for num, output_dict in output_collection.targets.items()
        }

        # loop over models to keep only one in memory at a time
        for fold_index, inps in models.items():
            with self.publish_step(f"\nloading model for fold {fold_index} ..."), get_device("cpu"):
                model = inps["saved_model"].load(formatter="tf_saved_model")

            # loop over files
            for num, output_dict in output_collection.targets.items():
                # load the input tree
                skim_file = self.get_skim_file(num)
                in_tree = skim_file.load(formatter="uproot")["HTauTauTree"]

                # read columns and insert dynamic ones
                arr = in_tree.arrays(list(columns_to_read), aliases=cfg.klub_aliases, library="ak")
                # prepare the output tree structure if not done yet (in case this is the first fold)
                for key, output in output_dict.items():
                    tmp_output = tmp_outputs[num][output]
                    if tmp_output.exists():
                        # read existing output columns that already evaluated on a previous fold
                        out_tree = tmp_output.load(formatter="uproot")["hbtres"].arrays()
                        out_tree = {
                            field: np.asarray(out_tree[field])
                            for field in out_tree.fields
                        }
                    else:
                        out_tree = {c: np.asarray(arr[c]) for c in cfg.klub_index_columns}

                    if "nominal" in key:
                        cont_inputs, cat_inputs, eval_mask = calc_inputs(arr, dyn_names, cfg, fold_index, models)
                        # evaluate the data
                        with self.publish_step(f"evaluating model for nominal on {eval_mask.sum()} events ..."):
                            out_tree = eval(model,cont_inputs, cat_inputs,
                                            self.spins, self.masses,
                                            eval_mask, class_names,
                                            "nominal", out_tree)

                        # save the output tree
                        with tmp_output.dump(formatter="uproot", mode="recreate") as f:
                            f["hbtres"] = out_tree
                    elif "shapes" in key:
                        # reduce array to only events that are in resolved1b, resolved2b or boosted category
                        if self.sample.year_flag == 0: # 2016APV
                            year = "2016APV"
                            category_mask = sel_cats(arr, year)
                            self.publish_message(f"events falling into categories: {ak.mean(category_mask) * 100:.2f}%")
                            syst_arr = arr[category_mask]
                        elif self.sample.year_flag == 1: # 2016
                            year = "2016"
                            category_mask = sel_cats(arr, year)
                            self.publish_message(f"events falling into categories: {ak.mean(category_mask) * 100:.2f}%")
                            syst_arr = arr[category_mask]
                        elif self.sample.year_flag == 2: # 2017
                            year = "2017"
                            category_mask = sel_cats(arr, year)
                            self.publish_message(f"events falling into categories: {ak.mean(category_mask) * 100:.2f}%")
                            syst_arr = arr[category_mask]
                        elif self.sample.year_flag == 3: # 2018
                            year = "2018"
                            category_mask = sel_cats(arr, year)
                            self.publish_message(f"events falling into categories: {ak.mean(category_mask) * 100:.2f}%")
                            syst_arr = arr[category_mask]

                        for shape_name in shape_names:
                            #if shape_systs[shape_name]["num_sources"] == 1:
                            for dst, src in shape_systs[shape_name].items():
                                if src in arr.fields:
                                    # easy case: just drop in the alias
                                    syst_arr = ak.with_field(syst_arr, syst_arr[src], dst)
                                cont_inputs, cat_inputs, eval_mask = calc_inputs(syst_arr, dyn_names, cfg, fold_index, models)
                                # evaluate the data
                                with self.publish_step(f"evaluating model for shape '{shape_name}' on {eval_mask.sum()} events ..."):
                                    out_tree = eval(model,cont_inputs, cat_inputs,
                                                    self.spins, self.masses,
                                                    eval_mask, class_names,
                                                    shape_name, out_tree)
                                    # update progress
                                    publish_progress(progress_step)
                                    progress_step += 1
                        # save the output tree
                        with tmp_output.dump(formatter="uproot", mode="recreate") as f:
                            f["hbtres"] = out_tree

        # free memory
        del models

        # move the temporary outputs to the final location, optionally inserting the original tree
        output_collection.dir.touch()
        for num, output_dict in tmp_outputs.items():
            for output, tmp_output in output_dict.items():
                tmp_output.move_to_local(output)


class EvaluateSkimsWrapper(MultiSkimTask, EvaluationParameters, law.WrapperTask):

    def requires(self):
        return {
            skim_name: EvaluateSkims.req(self, skim_name=skim_name)
            for skim_name in self.skim_names
        }


_default_categories = ("2017_*tau_resolved?b_os_iso", "2017_*tau_boosted_os_iso", "2017_*tau_vbf_os_iso")


class WriteDatacards(MultiSkimTask, EvaluationParameters):

    categories = law.CSVParameter(
        default=_default_categories,
        description=f"comma-separated patterns of categories to produce; default: {','.join(_default_categories)}",
        brace_expand=True,
    )
    qcd_estimation = luigi.BoolParameter(
        default=True,
        description="whether to estimate QCD contributions from data; default: True",
    )
    binning = luigi.ChoiceParameter(
        default="flats",
        choices=("flats", "equal", "ud", "ud_flats", "tt_dy_driven"),
        description=(
            "binning to use; choices: flats, equal, ud (uncertainty-driven) "
            "ud_flats (uncertainty-driven with flat signal distribution), tt_dy_driven (tt+dy-driven) "
            "default: flats"),
    )
    n_bins = luigi.IntParameter(
        default=10,
        description="number of bins to use; default: 10",
    )
    uncertainty = luigi.FloatParameter(
        default=0.1,
        description="uncertainty to use for the ud binning; default: 0.1",
    )
    signal_uncertainty = luigi.FloatParameter(
        default=0.5,
        description="signal uncertainty to use for uncertainty-driven and tt_dy_driven binning; default: 0.5",
    )
    variable = luigi.Parameter(
        default="hbtresdnn_mass{mass}_spin{spin}_hh",
        description="variable to use; template values 'mass' and 'spin' are replaced automatically; "
        "default: 'hbtresdnn_mass{mass}_spin{spin}_hh'",
    )
    parallel_read = luigi.IntParameter(
        default=4,
        description="number of parallel processes to use for reading; default: 4",
    )
    parallel_write = luigi.IntParameter(
        default=4,
        description="number of parallel processes to use for writing; default: 4",
    )
    output_suffix = luigi.Parameter(
        default=law.NO_STR,
        description="suffix to append to the output directory; default: ''",
    )
    rewrite_existing = luigi.BoolParameter(
        default=False,
        significant=False,
        description="whether to rewrite existing datacards; default: False",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.card_pattern = "cat_{category}_spin_{spin}_mass_{mass}"
        self._card_names = None

    @property
    def card_names(self):
        if self._card_names is None:
            from tautaunn.write_datacards import expand_categories
            categories = expand_categories(self.categories)
            self._card_names = [
                self.card_pattern.format(category=category, spin=spin, mass=mass)
                for spin, mass, category in itertools.product(self.spins, self.masses, categories)
            ]

        return self._card_names

    def requires(self):
        return {
            skim_name: EvaluateSkims.req(self, skim_name=skim_name)
            for skim_name in self.skim_names
        }

    def store_parts(self):
        parts = super().store_parts()
        parts.insert_before("version", "ensemble", self.get_model_name())
        return parts

    def output(self):
        # prepare the output directory
        if self.binning in ["flats", "equal"]:
            dirname = f"{self.binning}{self.n_bins}"
        else:
            dirname = f"{self.binning}{self.n_bins}_{self.uncertainty}_{self.signal_uncertainty}"
        if self.output_suffix not in ("", law.NO_STR):
            dirname += f"_{self.output_suffix.lstrip('_')}"
        d = self.local_target(dirname, dir=True)

        return law.SiblingFileCollection({
            name: {
                "datacard": d.child(f"datacard_{name}.txt", type="f"),
                "shapes": d.child(f"shapes_{name}.root", type="f"),
            }
            for name in self.card_names
        })

    @law.decorator.safe_output
    def run(self):
        # load the datacard creating function
        from tautaunn.write_datacards_stack import write_datacards

        # prepare inputs
        inp = self.input()

        # prepare skim and eval directories, and samples to use per
        skim_directories = defaultdict(list)
        eval_directories = {}
        for skim_name in inp:
            sample = cfg.get_sample(skim_name, silent=True)
            if sample is None:
                sample_name, skim_year = self.split_skim_name(skim_name)
                sample = cfg.Sample(sample_name, year=skim_year)
            skim_directories[(sample.year, cfg.skim_dirs[sample.year])].append(sample.name)
            if sample.year not in eval_directories:
                eval_directories[sample.year] = inp[skim_name].collection.dir.parent.path

        # define arguments
        datacard_kwargs = dict(
            spin=list(self.spins),
            mass=list(self.masses),
            category=list(self.categories),
            skim_directories=skim_directories,
            eval_directories=eval_directories,
            output_directory=self.output().dir.path,
            output_pattern=self.card_pattern,
            variable_pattern=self.variable,
            # force using all samples, disabling the feature to select a subset
            # sample_names=[sample_name.replace("SKIM_", "") for sample_name in sample_names],
            binning=(self.n_bins, 0.0, 1.0, self.binning),
            uncertainty=self.uncertainty,
            signal_uncertainty=self.signal_uncertainty,
            qcd_estimation=self.qcd_estimation,
            n_parallel_read=self.parallel_read,
            n_parallel_write=self.parallel_write,
            cache_directory=os.path.join(os.environ["TN_DATA_DIR"], "datacard_cache"),
            skip_existing=not self.rewrite_existing,
        )

        # create the cards
        write_datacards(**datacard_kwargs)
