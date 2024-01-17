
import luigi
import law

from tautaunn.tasks.datacards import WriteDatacards, EvaluationParameters
from tautaunn.tasks.base import MultiSkimTask


class MakeFinalDistPlots(MultiSkimTask, EvaluationParameters):

    limits_file = luigi.Parameter(
        default="", 
        # This should come from PlotResonantLimits somehow.. no idea how to do that
        description="limits will be read from this /path/to/limits_file.npz> "
    )
    input_dir = luigi.Parameter(
        default="",
        # TODO: This location should already be determined by the WriteDatacards tasks
        # Can I retrieve the location from WriteDatacards somehow?
        description="Directory, where shapes.root files are stored by WriteDatacards"
    )
    output_dir = luigi.Parameter(
        default="",
        description="full path to dir, where plot files will be stored."
    )
    output_suffix = luigi.Parameter(
        default=law.NO_STR,
        description="suffix to append to the output directory; default: ''",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def requires(self):
        return WriteDatacards.req(self)

    def output(self):
        # prepare the output directory
        dirname = f"{self.output_dir}"
        if self.output_suffix not in ("", law.NO_STR):
            dirname += f"_{self.output_suffix.lstrip('_')}"
        return self.local_target(dirname, dir=True)

    @law.decorator.safe_output
    def run(self):
        # load the datacard creating function
        from tautaunn.plot_dists import make_plots

        # prepare inputs
        # inp = self.input()
        # sample_names = list(inp)

        # create the plots
        make_plots(
            limits_file=self.limits_file,
            input_dir=self.input_dir,
            output_dir=self.output_dir,
        )
