python train.py --multirun hydra/launcher=mila_eric save_dir=/home/mila/e/eric.elmoznino/scratch/prequential_icl/logs seed=0,1,2,3,4 dataset=regression/linear,regression/sinusoid task=sgd_optimizer ++logger.tags=[experiments/sgd_vs_prequential/regression]
