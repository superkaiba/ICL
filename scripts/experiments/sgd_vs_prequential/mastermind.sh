python train.py --multirun hydra/launcher=mila_eric save_dir=/home/mila/e/eric.elmoznino/scratch/prequential_icl/logs seed=0,1,2,3,4 dataset=symbolic/mastermind task=sgd_optimizer ++task.loss_fn._target_=utils.CrossEntropyLossFlat ++predictor.in_features=48 ++predictor.out_features=18 ++predictor.h_dim=256 ++dataset.train_dataset.one_hot_y=False ++dataset.train_dataset.n_samples=3000 ++logger.tags=[experiments/sgd_vs_prequential/symbolic]
