from __future__ import annotations

import logging

import lightning as L
import torch
from torch.autograd.anomaly_mode import set_detect_anomaly

from pst.predict import model_inference
from pst.training import cv, full
from pst.training.tuning import tuning
from pst.utils.cli import Args, parse_args
from pst.utils.cli.modes import InferenceMode, TrainingMode, TuningMode
from pst.utils.download import download
from pst.utils.graphify import to_graph_format
from pst.utils.history import update_config_from_history

_SEED = 111
logger = logging.getLogger(__name__)


def train_main(args: TrainingMode):
    args = update_config_from_history(args)
    if args.data.train_on_full:
        full.train_with_all_data(args)
    else:
        L.seed_everything(_SEED)
        cv.train_with_cross_validation(args)


def tune_main(args: TuningMode):
    tuning.tune(args)


def test_main(args: Args):
    raise NotImplementedError


def predict_main(args: InferenceMode):
    L.seed_everything(_SEED)
    model_inference(args, True, True, False)


def _check_cpu_accelerator(config: TrainingMode):
    if config.trainer.accelerator == "cpu":
        threads = config.trainer.devices
        config.trainer.precision = "32"
        torch.set_num_threads(threads)
        config.trainer.devices = 1
        config.trainer.strategy = "auto"


def main():
    args = parse_args()

    if args.graphify is not None:
        to_graph_format(args.graphify.graphify)
        return
    elif args.download is not None:
        download(args.download.download)
        return
    elif args.train is not None:
        config = args.train
        _check_cpu_accelerator(config)
        fn = train_main
    elif args.tune is not None:
        config = args.tune
        _check_cpu_accelerator(config)
        fn = tune_main
    elif args.predict is not None:
        config = args.predict
        _check_cpu_accelerator(config)
        fn = predict_main
    else:
        raise RuntimeError(
            "Must pass either 'train', 'tune', or 'predict' as running mode."
        )

    with set_detect_anomaly(config.experiment.detect_anomaly):
        if config.experiment.detect_anomaly:
            logger.warning(
                "Anomaly detection mode is on. This will be slow since the autograd "
                "engine has to check for gradient anomalies."
            )

        fn(config)  # type: ignore


if __name__ == "__main__":
    main()
