__all__ = ['Target', 'Fit', 'MODELS']

import copy as cp
from pylab import *
from .data import *
from . import qnms
from . import injection
import lal
from collections import namedtuple
import pkg_resources
import arviz as az
from ast import literal_eval
from inspect import getfullargspec

# def get_raw_time_ifo(tgps, raw_time, duration=None, ds=None):
#     ds = ds or 1
#     duration = inf if duration is None else duration
#     m = abs(raw_time - tgps) < 0.5*duration
#     i = argmin(abs(raw_time - tgps))
#     return roll(raw_time, -(i % ds))[m]

Target = namedtuple('Target', ['t0', 'ra', 'dec', 'psi'])

MODELS = ('ftau', 'mchi', 'mchi_aligned')

class Fit(object):
    """ A ringdown fit.

    Attributes
    ----------
    model : str
        name of Stan model to be fit.
    data : dict
        dictionary containing data, indexed by detector name.
    acfs : dict
        dictionary containing autocovariance functions corresponding to data,
        if already computed.
    start_times : dict
        target truncation time for each detector.
    antenna_patterns : dict
        dictionary of tuples (Fp, Fc) with plus and cross antenna patterns
        for each detector (only applicable depending on model).
    target : Target
        information about truncation time at geocenter and, if applicable,
        source right ascension, declination and polarization angle.
    result : arviz.data.inference_data.InferenceData
        if model has been run, arviz object containing fit result
    prior : arviz.data.inference_data.InferenceData
        if model prior has been run, arviz object containing prior
    modes : list
        if applicable, list of (p, s, l, m, n) tuples identifying modes to be
        fit (else, None).
    n_modes : int
        number of modes to be fit.
    ifos : list
        list of detector names.
    t0 : float
        target geocenter start time.
    sky : tuple
        tuple with source right ascension, declination and polarization angle.
    analysis_data : dict
        dictionary of truncated analysis data that will be fed to Stan model.
    spectral_coefficients : tuple
        tuple of arrays containing dimensionless frequency and damping rate
        fit coefficients to be passed internally to Stan model.
    model_data : dict
        arguments passed to Stan model internally.
    """


    _compiled_models = {}

    def __init__(self, model='mchi', modes=None, **kws):
        self.data = {}
        self.injections = {}
        self.acfs = {}
        self.start_times = {}
        self.antenna_patterns = {}
        self.target = Target(None, None, None, None)
        if model.lower() in MODELS:
            self.model = model.lower()
        else:
            raise ValueError('invalid model {:s}; options are {}'.format(model,
                                                                         MODELS))
        self.result = None
        self.prior = None
        self._duration = None
        self._n_analyze = None
        self.injection_parameters = {}
        # set modes dynamically
        self._nmodes = None
        self.modes = None
        self.set_modes(modes)
        # assume rest of kwargs are to be passed to stan_data (e.g. prior)
        self._prior_settings = kws

    @property
    def n_modes(self) -> int:
        """ Number of damped sinusoids to be included in template.
        """
        return self._n_modes or len(self.modes)

    @property
    def _model(self):
        if self.model is None:
            raise ValueError('you must specify a model')
        elif self.model not in self._compiled_models:
            if self.model in MODELS:
                self.compile()
            else:
                raise ValueError('unrecognized model %r' % self.model)
        return self._compiled_models[self.model]

    def compile(self, verbose=False, force=False):
        """ Compile `Stan` model.

        Arguments
        ---------
        verbose : bool
            print out all messages from compiler.
        force : bool
            force recompile.
        """
        if force or self.model not in self._compiled_models:
            # compile model and cache in class variable
            code = pkg_resources.resource_string(__name__,
                'stan/ringdown_{}.stan'.format(self.model)
            )
            import pystan
            kws = dict(model_code=code.decode("utf-8"))
            if not verbose:
                kws['extra_compile_args'] = ["-w"]
            self._compiled_models[self.model] = pystan.StanModel(**kws)

    @property
    def ifos(self) -> list:
        """ Instruments to be analyzed.
        """
        return list(self.data.keys())

    @property
    def t0(self) -> float:
        """ Target truncation time (defined at geocenter if model accepts
        multiple detectors).
        """
        return self.target.t0

    @property
    def sky(self) -> tuple:
        """ Tuple of source right ascension, declination and polarization angle
        (all in radians). This can be set using
        :meth:`ringdown.fit.Fit.set_target`.
        """
        return (self.target.ra, self.target.dec, self.target.psi)

    # this can be generalized for charged bhs based on model name
    @property
    def spectral_coefficients(self) -> tuple:
        """Regression coefficients used by sampler to obtain mode frequencies
        and damping times as a function of physical black hole parameters.
        """
        f_coeffs = []
        g_coeffs = []
        for mode in self.modes:
            coeffs = qnms.KerrMode(mode).coefficients
            f_coeffs.append(coeffs[0])
            g_coeffs.append(coeffs[1])
        return array(f_coeffs), array(g_coeffs)

    @property
    def analysis_data(self) -> dict:
        """Slice of data to be analyzed for each detector. Extracted from
        :attr:`ringdown.fit.Fit.data` based on information in analysis target
        :attr:`ringdown.fit.Fit.target`.
        """
        data = {}
        i0s = self.start_indices
        for i, d in self.data.items():
            data[i] = d.iloc[i0s[i]:i0s[i] + self.n_analyze]
        return data

    @property
    def _default_prior(self):
        # turn off ACF drift correction by default.
        default = {'A_scale': None, 'drift_scale': 0.0}
        if self.model == 'ftau':
            # TODO: set default priors based on sampling rate and duration
            default.update(dict(
                f_max=None,
                f_min=None,
                gamma_max=None,
                gamma_min=None
            ))
        elif self.model == 'mchi':
            default.update(dict(
                perturb_f=zeros(self.n_modes or 1),
                perturb_tau=zeros(self.n_modes or 1),
                df_max=0.5,
                dtau_max=0.5,
                M_min=None,
                M_max=None,
                chi_min=0,
                chi_max=0.99,
                flat_A_ellip=0
            ))
        elif self.model == 'mchi_aligned':
            default.update(dict(
                perturb_f=zeros(self.n_modes or 1),
                perturb_tau=zeros(self.n_modes or 1),
                df_max=0.5,
                dtau_max=0.5,
                M_min=None,
                M_max=None,
                chi_min=0,
                chi_max=0.99,
                cosi_min=-1,
                cosi_max=1,
                flat_A=0
            ))
        return default

    @property
    def prior_settings(self) -> dict:
        """Prior options as currently set.
        """
        prior = self._default_prior
        prior.update(self._prior_settings)
        return prior

    @property
    def valid_model_options(self) -> list:
        """Valid prior parameters for the selected model. These can be set
        through :meth:`ringdown.fit.Fit.update_prior`.
        """
        return list(self._default_prior.keys())

    # TODO: warn or fail if self.results is not None?
    def update_prior(self, **kws):
        """Set or modify prior options.  For example,
        ``fit.update_prior(A_scale=1e-21)`` sets the `A_scale` parameter to
        `1e-21`.

        Valid arguments for the selected model can be found in
        :attr:`ringdown.fit.Fit.valid_model_options`.
        """
        valid_keys = self.valid_model_options
        valid_keys_low = [k.lower() for k in valid_keys]
        for k, v in kws.items():
            if k.lower() in valid_keys_low:
                i = valid_keys_low.index(k.lower())
                self._prior_settings[valid_keys[i]] = v
            else:
                raise ValueError('{} is not a valid model argument.'
                                 'Valid options are: {}'.format(k, valid_keys))

    @property
    def model_input(self) -> dict:
        """Arguments to be passed to sampler.
        """
        if not self.acfs:
            print('WARNING: computing ACFs with default settings.')
            self.compute_acfs()

        data_dict = self.analysis_data

        stan_data = dict(
            # data related quantities
            nsamp=self.n_analyze,
            nmode=self.n_modes,
            nobs=len(data_dict),
            t0=list(self.start_times.values()),
            times=[d.time for d in data_dict.values()],
            strain=list(data_dict.values()),
            L=[acf.iloc[:self.n_analyze].cholesky for acf in self.acfs.values()],
            FpFc = list(self.antenna_patterns.values()),
            # default priors
            dt_min=-1E-6,
            dt_max=1E-6
        )

        if 'mchi' in self.model:
            f_coeff, g_coeff = self.spectral_coefficients
            stan_data.update(dict(
                f_coeffs=f_coeff,
                g_coeffs=g_coeff,
        ))

        stan_data.update(self.prior_settings)

        for k, v in stan_data.items():
            if v is None:
                raise ValueError('please specify {}'.format(k))
        return stan_data

    @classmethod
    def from_config(cls, config_input):
        """Creates a :class:`Fit` instance from a configuration file.
        
        Has the ability to load and condition data, as well as to compute or
        load ACFs. Does not run the fit automatically.

        Arguments
        ---------
        config_input : str, configparser.ConfigParser
            path to config file on disk, or preloaded
            :class:`configparser.ConfigParser`

        Returns
        -------
        fit : Fit
            Ringdown :class:`Fit` object.
        """
        import configparser
        if isinstance(config_input, configparser.ConfigParser):
            config = config_input
        else:
            config = configparser.ConfigParser()
            config.read(config_input)
        # utility function
        def try_float(x):
            try:
                return float(x)
            except (TypeError,ValueError):
                return x
        # create fit object
        fit = cls(config['model']['name'], modes=config['model']['modes'])
        # add priors
        fit.update_prior(**{k: float(v) for k,v in config['prior'].items()})
        if 'data' not in config:
            # the rest of the options require loading data, so if no pointer to
            # data was provided, just exit
            return fit
        # load data
        # TODO: add ability to generate synthetic data here?
        ifo_input = config.get('data', 'ifos', fallback='')
        ifos = [i.strip().strip('[').strip(']') for i in ifo_input.split(',')]
        path_input = config['data']['path']
        # NOTE: not popping in order to preserve original ConfigParser
        used_keys = ['ifos', 'data', 'path']
        kws = {k: v for k,v in config['data'].items() if k not in used_keys}
        for ifo in ifos:
            i = '' if not ifo else ifo[0]
            path = path_input.format(i=i, ifo=ifo)
            fit.add_data(Data.read(path, ifo=ifo, **kws))
        # add target
        target = config['target']
        fit.set_target(**{k: literal_eval(v) for k,v in target.items()})
        # condition data if requested
        if config.has_section('condition'):
            cond_kws = {k: try_float(v) for k,v in config['condition'].items()}
            fit.condition_data(**cond_kws)
        # load or produce ACFs
        if config.get('acf', 'path', fallback=False):
            kws = {k: v for k,v in config['acf'].items() if k not in ['path']}
            kws['header'] = kws.get('header', None)
            for ifo in ifos:
                p = config['acf']['path'].format(i=i, ifo=ifo)
                fit.acfs[ifo] = AutoCovariance.read(p, **kws)
        else:
            acf_kws = {} if 'acf' not in config else config['acf']
            fit.compute_acfs(**{k: try_float(v) for k,v in acf_kws.items()})
        return fit

    def copy(self):
        """Produce a deep copy of this `Fit` object.

        Returns
        -------
        fit_copy : Fit
            deep copy of `Fit`.
        """
        return cp.deepcopy(self)

    def condition_data(self, **kwargs):
        """Condition data for all detectors by calling
        :meth:`ringdown.data.Data.condition`. Docstring for that function below.

        """
        new_data = {}
        for k, d in self.data.items():
            t0 = self.start_times[k]
            new_data[k] = d.condition(t0=t0, **kwargs)

        self.data = new_data
        self.acfs = {} # Just to be sure that these stay consistent
    condition_data.__doc__ += Data.condition.__doc__

    def inject(self, fast_projection=False, window='auto', **kws):
        """Add simulated signal to data.
        """
        if window == 'auto':
            if self.duration is not None:
                kws['window'] = 10*self.duration
        else:
            kws['window'] = window
        all_kws = {k: v for k,v in locals().items() if k not in ['self']}
        all_kws.update(all_kws.pop('kws'))
        s_kws = all_kws.copy()
        p_kws ={k: s_kws.pop(k) for k in kws.keys() if k in 
                getfullargspec(injection.Signal.project)[0][1:]}
        if all([k in p_kws for k in ['ra', 'dec']]):
            aps = {}
        else:
            aps = p_kws.pop('antenna_patterns', None) or self.antenna_patterns

        if fast_projection:
            # evaluate template once and timeshift for each detector
            s_kws['t0'] = all_kws.get('t0', self.t0)
            p_kws['delay'] = all_kws.get('delay', 'from_geo')
            for k in ['ra', 'dec', 'psi']:
                p_kws[k] = p_kws.get(k, self.target._asdict()[k])
            print(p_kws)
            # get baseline signal (by default at geocenter)
            t = self.data[self.ifos[0]].time.values
            gw = injection.Ringdown.from_parameters(t, **s_kws)
            # project onto each detector
            self.injections = {i: gw.project(antenna_patterns=aps.get(i, None),
                                             ifo=i, **p_kws)
                               for i in self.ifos}
        else:
            # revaluate the template from scratch for each detector
            if 't0' not in all_kws:
                p_kws['delay'] = None
            for ifo, d in self.data.items():
                s_kws['t0'] = all_kws.get('t0', self.start_times[ifo])
                gw = injection.Ringdown.from_parameters(d.time.values,
                                                        **s_kws)
                self.injections[ifo] = gw.project(antenna_patterns=aps[ifo],
                                                  ifo=ifo, **p_kws)
        for i, h in self.injections.items():
            self.data[i] = self.data[i] + h
        self.injection_parameters = all_kws
        return gw

    def run(self, prior=False, **kws):
        """Fit model.

        Additional keyword arguments not listed below are passed to
        :func:`pystan.model.sampling`.

        Arguments
        ---------
        prior : bool
            whether to sample the prior (def. False).
        """
        #check if delta_t of ACFs is equal to delta_t of data
        for ifo in self.ifos:
            if self.acfs[ifo].delta_t != self.data[ifo].delta_t:
                e = "{} ACF delta_t ({:.1e}) does not match data ({:.1e})."
                raise ValueError(e.format(ifo, self.acfs[ifo].delta_t,
                                          self.data[ifo].delta_t))
        # get model input
        stan_data = self.model_input
        stan_data['only_prior'] = int(prior)
        # get sampler settings
        n = kws.pop('thin', 1)
        chains = kws.pop('chains', 4)
        n_jobs = kws.pop('n_jobs', chains)
        n_iter = kws.pop('iter', 2000*n)
        metric = kws.pop('metric', 'dense_e')
        adapt_delta = kws.pop('adapt_delta', 0.8)
        stan_kws = {
            'iter': n_iter,
            'thin': n,
            'init': (kws.pop('init_dict', {}),)*chains,
            'n_jobs': n_jobs,
            'chains': chains,
            'control': {'metric': metric, 'adapt_delta': adapt_delta}
        }
        stan_kws.update(kws)
        # run model and store
        print('Running {}'.format(self.model))
        result = self._model.sampling(data=stan_data, **stan_kws)
        if prior:
            self.prior = az.convert_to_inference_data(result)
        else:
            od = {'strain': self.model_input['strain']}
            cd = {k: v for k,v in self.model_input.items() if k != 'strain'}
            self.result = az.convert_to_inference_data(result, observed_data=od,
                                                       constant_data=cd)

    def add_data(self, data, time=None, ifo=None, acf=None):
        """Add data to fit.

        Arguments
        ---------
        data : array,Data
            time series to be added.
        time : array
            array of time stamps (only required if `data` is not
            :class:`ringdown.data.Data`).
        ifo : str
            interferometer key (optional).
        acf : array,AutoCovariance
            autocovariance series corresponding to these data (optional).
        """
        if not isinstance(data, Data):
            data = Data(data, index=getattr(data, 'time', time), ifo=ifo)
        self.data[data.ifo] = data
        if acf is not None:
            self.acfs[data.ifo] = acf

    def compute_acfs(self, shared=False, ifos=None, **kws):
        """Compute ACFs for all data sets in `Fit.data`.

        Arguments
        ---------
        shared : bool
            specifices if all IFOs are to share a single ACF, in which case the
            ACF is only computed once from the data of the first IFO (useful
            for simulated data) (default False)

        ifos : list
            specific set of IFOs for which to compute ACF, otherwise computes
            it for all

        extra kwargs are passed to ACF constructor
        """
        ifos = self.ifos if ifos is None else ifos
        if len(ifos) == 0:
            raise ValueError("first add data")
        # if shared, compute a single ACF
        acf = self.data[ifos[0]].get_acf(**kws) if shared else None
        for ifo in ifos:
            self.acfs[ifo] = acf if shared else self.data[ifo].get_acf(**kws)

    def set_tone_sequence(self, nmode, p=1, s=-2, l=2, m=2):
        """ Set template modes to be a sequence of overtones with a given
        angular structure.

        To set an arbitrary set of modes, use :meth:`ringdown.fit.Fit.set_modes`

        Arguments
        ---------
        nmode : int
          number of tones (`nmode=1` includes only fundamental mode).
        p : int
          prograde (`p=1`) vs retrograde (`p=-1`) flag.
        s : int
          spin-weight.
        l : int
          azimuthal quantum number.
        m : int
          magnetic quantum number.
        """
        indexes = [(p, s, l, m, n) for n in range(nmode)]
        self.set_modes(indexes)

    def set_modes(self, modes):
        """ Establish list of modes to include in analysis template.

        Modes identified by their `(p, s, l, m, n)` indices, where:
          - `p` is `1` for prograde modes, and `-1` for retrograde modes;
          - `s` is the spin-weight (`-2` for gravitational waves);
          - `l` is the azimuthal quantum number;
          - `m` is the magnetic quantum number;
          - `n` is the overtone number.

        See :meth:`ringdown.qnms.construct_mode_list`.

        Arguments
        ---------
        modes : list
            list of tuples with quasinormal mode `(p, s, l, m, n)` numbers.
        """
        try:
            # if modes is integer, interpret as number of modes
            self._n_modes = int(modes)
            self.modes = None
        except (TypeError, ValueError):
            # otherwise, assume it is a mode index list
            self._n_modes = None
            self.modes = qnms.construct_mode_list(modes)
            if self.model == 'mchi_aligned':
                ls_valid = [mode.l == 2 for mode in self.modes]
                ms_valid = [abs(mode.m) == 2 for mode in self.modes]
                if not (all(ls_valid) and all(ms_valid)):
                    raise ValueError("mchi_aligned model only accepts l=m=2 modes")

    def set_target(self, t0, ra=None, dec=None, psi=None, delays=None,
                   antenna_patterns=None, duration=None, n_analyze=None):
        """ Establish truncation target, stored to `self.target`.

        Provide a targetted analysis start time `t0` to serve as beginning of
        truncated analysis segment; this will be compared against timestamps
        in `fit.data` objects so that the closest sample to `t0` is preserved
        after conditioning and taken as the first sample of the analysis 
        segment.

        .. important::
          If the model accepts multiple detectors, `t0` is assumed to be
          defined at geocenter; truncation time at individual detectors will
          be determined based on specified sky location.

        The source sky location and orientation can be specified by the `ra`,
        `dec`, and `psi` arguments. These are use to both determine the
        truncation time at different detectors, as well as to compute the 
        corresponding antenna patterns. Specifying a sky location is only
        required if the model can handle data from multiple detectors.

        Alternatively, antenna patterns and geocenter-delays can be specified
        directly through the `antenna_patterns` and `delays` arguments.

        For all models, the argument `duration` specifies the length of the 
        analysis segment in the unit of time used to index the data (e.g., s).
        Based on the sampling rate, this argument is used to compute the number
        of samples to be included in the segment, beginning from the first
        sample identified from `t0`.

        Alternatively, the `n_analyze` argument can be specified directly. If
        neither `duration` nor `n_analyze` are provided, the duration will be
        set based on the shortest available data series in the `Fit` object.

        .. warning::
          Failing to explicitly specify `duration` or `n_analyze` risks
          inadvertedly extremely long analysis segments, with correspondingly
          long runtimes.

        Arguments
        ---------
        t0 : float
            target time (at geocenter for a detector network).
        ra : float
            source right ascension (rad).
        dec : float
            source declination (rad).
        psi : float
            source polarization angle (rad).
        duration : float
            analysis segment length in seconds, or time unit indexing data
            (overrides `n_analyze`).
        n_analyze : int
            number of datapoints to include in analysis segment.
        delays : dict
            dictionary with delayes from geocenter for each detector, as would
            be computed by `lal.TimeDelayFromEarthCenter` (optional).
        antenna_patterns : dict
            dictionary with tuples for plus and cross antenna patterns for
            each detector `{ifo: (Fp, Fc)}` (optional)
        """
        if not self.data:
            raise ValueError("must add data before setting target.")
        tgps = lal.LIGOTimeGPS(t0)
        gmst = lal.GreenwichMeanSiderealTime(tgps)
        delays = delays or {}
        antenna_patterns = antenna_patterns or {}
        for ifo, data in self.data.items():
            # TODO: should we have an elliptical+ftau model?
            if ifo is None or self.model=='ftau':
                dt_ifo = 0
                self.antenna_patterns[ifo] = (1, 1)
            else:
                det = data.detector
                dt_ifo = delays.get(ifo,
                    lal.TimeDelayFromEarthCenter(det.location, ra, dec, tgps))
                self.antenna_patterns[ifo] = antenna_patterns.get(ifo,
                    lal.ComputeDetAMResponse(det.response, ra, dec, psi, gmst))
            self.start_times[ifo] = t0 + dt_ifo
        self.target = Target(t0, ra, dec, psi)
        # also specify analysis duration if requested
        if duration:
            self._duration = duration
        elif n_analyze:
            self._n_analyze = int(n_analyze)

    # TODO: warn or fail if self.results is not None?
    def update_target(self, **kws):
        """Modify analysis target. See also
        :meth:`ringdown.fit.Fit.set_target`.
        """
        target = self.target._asdict()
        target.update({k: getattr(self,k) for k in
                       ['duration', 'n_analyze', 'antenna_patterns']})
        target.update(kws)
        self.set_target(**target)

    @property
    def duration(self) -> float:
        """Analysis duration in the units of time presumed by the
        :attr:`ringdown.fit.Fit.data` and :attr:`ringdown.fit.Fit.acfs` objects
        (usually seconds). Defined as :math:`T = N\\times\Delta t`, where
        :math:`N` is the number of analysis samples
        (:attr:`ringdown.fit.n_analyze`) and :math:`\Delta t` is the time
        sample spacing.
        """
        if self._n_analyze and not self._duration:
            if self.data:
                return self._n_analyze*self.data[self.ifos[0]].delta_t
            else:
                print("Add data to compute duration (n_analyze = {})".format(
                      self._n_analyze))
                return None
        else:
            return self._duration

    @property
    def has_target(self) -> bool:
        """Whether an analysis target has been set with
        :meth:`ringdown.fit.Fit.set_target`.
        """
        return self.target.t0 is not None

    @property
    def start_indices(self) -> dict:
        """Locations of first samples in :attr:`ringdown.fit.Fit.data`
        to be included in the ringdown analysis for each detector.
        """
        i0_dict = {}
        if self.has_target:
            for ifo, d in self.data.items():
                t0 = self.start_times[ifo]
                i0_dict[ifo] = argmin(abs(d.time - t0))
        return i0_dict

    @property
    def n_analyze(self) -> int:
        """Number of data points included in analysis for each detector.
        """
        if self._duration and not self._n_analyze:
            # set n_analyze based on specified duration in seconds
            if self.data:
                dt = self.data[self.ifos[0]].delta_t
                return int(round(self._duration/dt))
            else:
                print("Add data to compute n_analyze (duration = {})".format(
                      self._duration))
                return None
        elif self.data and self.has_target:
            # set n_analyze to fit shortest data set
            i0s = self.start_indices
            return min([len(d.iloc[i0s[i]:]) for i, d in self.data.items()])
        else:
            return self._n_analyze

    def whiten(self, datas, drifts=None):
        """Return whiten data for all detectors.

        See also :meth:`ringdown.data.AutoCovariance.whiten`.

        Arguments
        ---------
        datas : dict
            dictionary of data to be whitened for each detector.
        drifts : dict
            optional ACF scale drift factors for each detector.

        Returns
        -------
        wdatas : dict
            dictionary of :class:`ringdown.data.Data` with whitned data for
            each detector.
        """
        if drifts is None:
            drifts = {i : 1 for i in datas.keys()}
        return {i: Data(self.acfs[i].whiten(d, drift=drifts[i]), ifo=i) 
                for i,d in datas.items()}
