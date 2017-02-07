import abc

import os, pickle, time
import numpy as np

from general.algorithms.prediction.prediction_model import PredictionModel

from config import params

from rll_quadrotor.policy.noise_models import ZeroNoise, GaussianNoise, UniformNoise, OUNoise, SmoothedGaussianNoise
from rll_quadrotor.utility.logger import get_logger
from rll_quadrotor.state_info.sample import Sample

class DaggerPrediction:
    __metaclass__ = abc.ABCMeta

    def __init__(self, read_only=False):
        self.use_cp_cost = True
        self.planner_type = params['prediction']['dagger']['planner_type']
        assert(self.planner_type == 'primitives' or
               self.planner_type == 'cem' or
               self.planner_type == 'ilqr' or
               self.planner_type == 'randomwalk' or
               self.planner_type == 'randomcontrolset' or
               self.planner_type == 'teleop' or
               self.planner_type == 'straight' or
               self.planner_type == 'lattice')
        self.read_only = read_only

        self._setup()

        self.logger = get_logger(self.__class__.__name__, 'info',
                                 os.path.join(self.bootstrap.save_dir, 'dagger.txt'))

    @abc.abstractmethod
    def _setup(self):
        ### load prediction neural net
        self.bootstrap = None

        self.cost_cp = None

        self.max_iter = None
        self.world = None
        self.dynamics = None
        self.agent = None
        self.trajopt = None
        self.conditions = None

        raise NotImplementedError('Implement in subclass')

    #####################
    ### World methods ###
    #####################

    @abc.abstractmethod
    def _reset_world(self, itr, cond, rep):
        raise NotImplementedError('Implement in subclass')

    @abc.abstractmethod
    def _update_world(self, sample, t):
        raise NotImplementedError('Implement in subclass')

    @abc.abstractmethod
    def _is_good_rollout(self, sample, t):
        raise NotImplementedError('Implement in subclass')

    #########################
    ### Create controller ###
    #########################

    @abc.abstractmethod
    def _create_mpc(self, itr, x0):
        raise NotImplementedError('Implement in subclass')

    ####################
    ### Save methods ###
    ####################

    @property
    def _save_dir(self):
        return os.path.abspath(self.bootstrap.save_dir)

    def _itr_dir(self, itr, create=True):
        assert(type(itr) is int)
        dir = os.path.join(self._save_dir, 'itr{0}'.format(itr))
        if not create:
            return dir
        if not os.path.exists(dir):
            os.makedirs(dir)
        return dir

    def _itr_samples_file(self, itr, create=True):
        return os.path.join(self._itr_dir(itr, create=create),
                            'samples_itr_{0}.npz'.format(itr))

    def _itr_stats_file(self, itr, create=True):
        return os.path.join(self._itr_dir(itr, create=create),
                            'stats_itr_{0}.pkl'.format(itr))

    def _itr_model_file(self, itr, create=True):
        return os.path.join(self._itr_dir(itr, create=create), 'model_itr_{0}.ckpt'.format(itr))

    def _itr_save_worlds(self, itr, world_infos):
        fname = os.path.join(self._itr_dir(itr), 'worlds_itr_{0}.pkl'.format(itr))
        with open(fname, 'w') as f:
            pickle.dump(world_infos, f)

    def _itr_save_mpcs(self, itr, mpc_infos):
        fname = os.path.join(self._itr_dir(itr), 'mpcs_itr_{0}.pkl'.format(itr))
        with open(fname, 'w') as f:
            pickle.dump(mpc_infos, f)

    def _itr_save_samples(self, itr, samples):
        Sample.save(self._itr_samples_file(itr), samples)

    @property
    def _img_dir(self):
        dir = os.path.join(self._save_dir, 'images')
        if not os.path.exists(dir):
            os.makedirs(dir)
        return dir

    def _img_file(self, itr, cond, rep):
        return os.path.join(self._img_dir, 'itr{0:03d}_cond{1:03d}_rep{2:03d}.png'.format(itr, cond, rep))

    @abc.abstractmethod
    def _get_world_info(self):
        raise NotImplementedError('Implement in subclass')

    ###################
    ### Run methods ###
    ###################

    def run(self):
        """
        for some iterations:
            gather data (i.e. samples)
                note: while gather data, record statistics to measure how well we're doing
            add new data to training data
            train neural network
        """
        ### find last model file
        for model_start_itr in xrange(self.max_iter-1, -1, -1):
            model_file = self._itr_model_file(model_start_itr, create=False)
            if PredictionModel.checkpoint_exists(model_file):
                model_start_itr += 1
                break
        else:
            model_file = None
            ### find last samples
        for samples_start_itr in xrange(self.max_iter-1, -1, -1):
            sample_file = self._itr_samples_file(samples_start_itr, create=False)
            if os.path.exists(sample_file):
                samples_start_itr += 1
                break
        ### should be within one
        assert (model_start_itr == samples_start_itr or
                model_start_itr == samples_start_itr - 1 or
                (model_start_itr == 1 and samples_start_itr == 0))

        ### load neural nets
        if model_file is not None:
            self.logger.info('Loading model {0}'.format(model_file))
            self.bootstrap.load(model_file=model_file)

        ### load previous data
        if samples_start_itr > 0:
            prev_sample_files = [self._itr_samples_file(itr) for itr in xrange(samples_start_itr)]
            assert (os.path.exists(f) for f in prev_sample_files)
            self.bootstrap.add_data(prev_sample_files)
        ### load initial dataset
        init_data_folder = params['prediction']['dagger'].get('init_data', None)
        if init_data_folder is not None:
            self.logger.info('Adding initial data')
            ext = os.path.splitext(self._itr_samples_file(0))[-1]
            fnames = [fname for fname in os.listdir(init_data_folder) if ext in fname]
            for fname in fnames:
                self.logger.info('\t{0}'.format(fname))
            self.bootstrap.add_data([os.path.join(init_data_folder, fname) for fname in fnames])

        ### if any data and haven't trained on it already, train on it
        if (model_start_itr == samples_start_itr - 1) or \
            (samples_start_itr == 0 and model_start_itr == 0 and init_data_folder is not None):
            old_model_file = None if samples_start_itr <= 0 else self._itr_model_file(model_start_itr)
            self.bootstrap.train(old_model_file=old_model_file,
                                 new_model_file=self._itr_model_file(model_start_itr),
                                 epochs=self.bootstrap.epochs)
        start_itr = samples_start_itr

        ### training loop
        for itr in xrange(start_itr, self.max_iter):
            self.logger.info('')
            self.logger.info('=== Itr {0}'.format(itr))
            self.logger.info('')

            self.logger.info('Itr {0} flying'.format(itr))
            self._run_itr(itr)

            if itr == 0 and False: # TODO
                # on first itr, don't want to train (so we can visualize)
                self.bootstrap.save(model_file=self._itr_model_file(itr))
            else:
                if self.planner_type != 'randomwalk':
                    self.logger.info('Itr {0} adding data'.format(itr))
                    self.bootstrap.add_data([self._itr_samples_file(itr)])
                    self.logger.info('Itr {0} training probability of collision'.format(itr))
                    self.bootstrap.train(old_model_file=self._itr_model_file(itr-1),
                                         new_model_file=self._itr_model_file(itr),
                                         epochs=self.bootstrap.epochs if itr > 1 else params['prediction']['dagger']['init_epochs'])

    def _run_itr(self, itr):
        T = params['prediction']['dagger']['T']
        label_with_noise = params['prediction']['dagger']['label_with_noise']

        samples = []
        world_infos = []
        mpc_infos = []

        self.conditions.reset()
        for cond in xrange(self.conditions.length):
            rep = 0
            while rep < self.conditions.repeats:
                # self._setup() # recreate everything so openrave doesn't get bogged down # TODO
                self.logger.info('\t\tStarting cond {0} rep {1}'.format(cond, rep))
                if (cond == 0 and rep == 0) or self.world.randomize:
                    self._reset_world(itr, cond, rep)

                x0 = self.conditions.get_cond(cond, rep=rep)
                sample_T = Sample(meta_data=params, T=T)
                sample_T.set_X(x0, t=0)

                mpc_policy = self._create_mpc(itr, x0)
                control_noise = self._create_control_noise() # create each time b/c may not be memoryless

                start = time.time()
                for t in xrange(T):
                    self.logger.info('\t\t\tt: {0}'.format(t))
                    # raw_input('Press enter to continue')
                    self._update_world(sample_T, t)

                    x0 = sample_T.get_X(t=t)

                    rollout = self.agent.sample_policy(x0, mpc_policy, noise=control_noise, T=1)

                    u = rollout.get_U(t=0)
                    o = rollout.get_O(t=0)

                    if label_with_noise or (control_noise is False):
                        u_label = u
                    else:
                        u_label = self.agent.sample_policy(x0, mpc_policy, noise=ZeroNoise(params), T=1).get_U(t=0)

                    sample_T.set_U(u_label, t=t)
                    sample_T.set_O(o, t=t)

                    if self.world.is_collision(sample_T, t=t):
                        self.logger.warning('\t\t\tCrashed at t={0}'.format(t))
                        break

                    if t < T-1:
                        x_tp1 = self.dynamics.evolve(x0, u)
                        sample_T.set_X(x_tp1, t=t+1)

                    if hasattr(mpc_policy, '_curr_traj'):
                        self.world.update_visualization(sample_T, mpc_policy._curr_traj, t)
                    # self.world.get_image(rollout)

                else:
                    self.logger.info('\t\t\tLasted for t={0}'.format(t))

                sample = sample_T.match(slice(0, t + 1))
                world_info = self._get_world_info()
                mpc_info = mpc_policy.get_info()

                if not self._is_good_rollout(sample, t):
                    self.logger.warning('\t\t\tNot good rollout. Repeating rollout.'.format(t))
                    continue

                samples.append(sample)
                world_infos.append(world_info)
                mpc_infos.append(mpc_info)

                assert(samples[-1].isfinite())
                elapsed = time.time() - start
                self.logger.info('\t\t\tFinished cond {0} rep {1} ({2:.1f}s, {3:.3f}x real-time)'.format(cond,
                                                                                                         rep,
                                                                                                         elapsed,
                                                                                                         t*params['dt']/elapsed))
                rep += 1

        self._itr_save_samples(itr, samples)
        self._itr_save_worlds(itr, world_infos)
        self._itr_save_mpcs(itr, mpc_infos)

        self._reset_world(itr, 0, 0) # leave the world as it was

    def _create_control_noise(self):
        cn_params = params['prediction']['dagger']['control_noise']

        if cn_params['type'] == 'zero':
            ControlNoiseClass = ZeroNoise
        elif cn_params['type'] == 'gaussian':
            ControlNoiseClass = GaussianNoise
        elif cn_params['type'] == 'uniform':
            ControlNoiseClass = UniformNoise
        elif cn_params['type'].lower() == 'ou':
            ControlNoiseClass = OUNoise
        elif cn_params['type'].lower() == 'smoothedgaussian':
            ControlNoiseClass = SmoothedGaussianNoise
        else:
            raise Exception('Control noise type {0} not valid'.format(cn_params['type']))

        return ControlNoiseClass(params, **cn_params[cn_params['type']])
