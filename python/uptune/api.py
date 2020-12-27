from future.utils import with_metaclass
from multiprocessing.pool import ThreadPool
import datetime
from collections import OrderedDict

import pandas as pd
import numpy as np
import abc, argparse, json, os, ray, logging
import threading, time, subprocess, copy, sys, signal

from uptune.src.prog_template import JinjaParser
from uptune.opentuner.api import TuningRunManager
from uptune.opentuner.measurement import MeasurementInterface
from uptune.opentuner.resultsdb.models import Result
from uptune.opentuner.search.manipulator import ConfigurationManipulator
from uptune.opentuner.search.manipulator import (
    IntegerParameter, EnumParameter, PowerOfTwoParameter, 
    LogIntegerParameter, BooleanParameter, FloatParameter,
    PermutationParameter 
)
from uptune.plugins.causaldiscovery import notears
from uptune.database.globalmodels import *

argparser = argparse.ArgumentParser(add_help=False)
argparser.add_argument('--timeout', type=int, default=72000,
                       help="auto-tuning timeout in seconds")
argparser.add_argument('--runtime-limit', '-rt', type=int, default=7200,
                       help="kill process if runtime exceeds the time in seconds")
argparser.add_argument('--async-interval', '-it', type=int, default=300,
                       help="interval in seconds for async scheduler to check the task queue")
argparser.add_argument('--parallel-factor', '-pf', type=int, default=2,
                       help="number of processes spawned by Parallel Python")
argparser.add_argument('--params-json', '-params', type=str, default="",
                       help="search space definition in json")
argparser.add_argument('--learning-models', '-lm', action="append", default=[],
                       help="single or ensemble of learning models for space pruning")
argparser.add_argument('--training-data', '-td', type=str, default='',
                       help="path to training data (support csv / txt)")
argparser.add_argument('--offline', action='store_true',
                       help="enable re-training for multi-stage")
argparser.add_argument('--aws', action='store_true', default=False,
                       help="use aws s3 storage for publishing")
argparser.add_argument('--cfg', action='store_true', default=False,
                       help="display configuration on screen")
argparser.add_argument('--gpu-num', type=int, default=0,
                       help="max number of gpu for each task")
argparser.add_argument('--cpu-num', type=int, default=1,
                       help="max number of cpu for each task")

log = logging.getLogger(__name__)

def init(apply_best=False): # reset uptune env variables 
    if not os.getenv("EZTUNING"):
        os.environ["UPTUNE"] = "True"
        if apply_best: # apply best cfg
            os.environ["BEST"] = "True"

# run with the best 
def get_best():
    assert os.path.isfile("ut-work-dir/best.json"), \
           "best cfg does not exsit"
    with open("ut-work-dir/best.json", "r") as fp:
        cfg, res = json.load(fp)
    fp.close()
    return cfg, res

class ParallelTuning(with_metaclass(abc.ABCMeta, object)):
    """Abstract class for parallel tuning"""
    def __init__(self,
                 cls,
                 args=None,
                 node='localhost',
                 parallel_factor=None):
  
        self.cls         = cls                     # ray actor class 
        self.args        = args                    # arguments for control
        self._parallel   = args.parallel_factor    # num of parallel instances 
        self._nodes      = tuple(node.split(','))
        self._limit      = args.test_limit 
        self._best       = list()
        self._cfg        = None
        self._pending    = list()                  # pending configs being validated
        self._prev       = False                   # whether recovering from history 
        self._valid      = False                   # whether pruning is enabled
        self._ratio      = 0.3                     # pruning score percentage threshold
        self._interval   = args.async_interval     # interval for checking the task pool
        self._mapping    = dict()                  # mappin from Enum to Int
        self._models     = list()                  # pretrained ML model list
        self._apis       = list()
        self._actors     = list()
        self._archive    = list()
        self._glbsession = list()
  
    def init(self): 
        path = 'ut-work-dir/uptune.db'
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        if self.args.database == None:
            self.args.database = 'sqlite:///ut-work-dir/global-'
  
    # After program dynamic analysis
    def before_run(self, copy=False):
        with open(self.args.params_json) as f:
            self._params = json.load(f)

        # If not reusing the parameters JSON
        # from last run, then move the params JSON into workdir
        if os.path.isfile("ut-tune-params.json"):
            os.system("mv ut-*.json ut-work-dir/")

        # create symbolic links
        # print("[ INFO ] Creating work folders...")
        work_dir = os.getenv("UT_WORK_DIR")
        for idx in range(self._parallel):
            thread_dir = "ut-work-dir/{}".format(str(idx))
            os.system("mkdir -p {} > /dev/null".format(thread_dir))
            os.chdir("{}".format(thread_dir))
            for f in os.listdir(work_dir):
                if not f.startswith("ut-"):
                    os.system("ln -s {}/{} .".format(work_dir, f))
            os.chdir(work_dir)

        # clean and move to ut-work-dir
        os.chdir('ut-work-dir')

        # init ray cluster 
        # ray.init(redis_address="localhost:6379")
        ray.init(logging_level=logging.FATAL)
  
  
    def create_tuning(self, index, stage, manipulator):
        args = self.args
        args.database = "sqlite:///" + os.path.join('uptune.db', 
                                                    str(index) + \
                                                    '-' + str(stage) + '.db')
        # keep meas-interface for tuners
        interface = MeasurementInterface(args=args,
                        manipulator=manipulator,
                        project_name='tuning',
                        program_name='tuning',
                        program_version='0.1')
        api = TuningRunManager(interface, args)
        return api
  
  
    def global_report(self, stage, epoch, api, node, cfg, requestor, result, flag=False):
        if result < self._best[stage] or self._best[stage] == None:
            self._best[stage] = result
            if stage == 0: # save best cfg
                self._cfg = cfg
                with open("best.json", "w") as fp:
                    json.dump([cfg, result], fp)
            flag = True
  
        # remove the config from pending list
        if requestor != "seed":
            assert cfg in self._pending, str(self._pending)
            self._pending.remove(cfg)

        api.manipulator.normalize(cfg)
        hashv = api.manipulator.hash_config(cfg)
        g = GlobalResult(epoch = epoch, 
                         node  = node, 
                         data  = cfg,
                         hashv = hashv,
                         time  = datetime.datetime.now(),
                         technique = requestor,
                         result = result,
                         was_the_best = flag)  
        self._glbsession[stage].add(g)
        self._glbsession[stage].flush()
        self._glbsession[stage].commit()
        return g
  
    def synchronize(self, stage, api, node, epoch):
        """ Synchronize results between different bandits """
        if epoch == 0: pass
        # Get the results from the same epoch of other nodes
        q = GlobalResult.extract(self._glbsession[stage], node, epoch) 
        api.sync(q)
  
    def create_params(self, stage=0):
        manipulator = ConfigurationManipulator()
        for item in self._params[stage]:
            ptype, pname, prange = item
            if ptype == "IntegerParameter": 
                manipulator.add_parameter(IntegerParameter(pname, prange[0], prange[1]))
            elif ptype == "EnumParameter": 
                manipulator.add_parameter(EnumParameter(pname, prange))
                self._mapping[pname] = dict([(y,x) for x,y in enumerate(set(prange))])
            elif ptype == "FloatParameter": 
                manipulator.add_parameter(FloatParameter(pname, prange[0], prange[1]))
            elif ptype == "LogIntegerParameter": 
                manipulator.add_parameter(LogIntegerParameter(pname, prange[0], prange[1]))
            elif ptype == "PowerOfTwoParameter": 
                manipulator.add_parameter(PowerOfTwoParameter(pname, prange[0], prange[1]))
            elif ptype == "BooleanParameter": 
                manipulator.add_parameter(BooleanParameter(pname))
            elif ptype == "PermutationParameter": 
                manipulator.add_parameter(PermutationParameter(pname, prange))
            else: assert False, "unrecognized type " + ptype
        return manipulator
           
  
    # Program executor for profiling before tuning 
    def call_program(self, cmd, limit=None, memory_limit=None):
        kwargs = dict()
        subenv = os.environ.copy()
        subenv["UT_BEFORE_RUN_PROFILE"] = "On"
        if limit is float('inf'):
            limit = None
        if type(cmd) in (str, str):
            kwargs['shell'] = True
            kwargs['env'] = subenv
            
        killed = False
        t0 = time.time()

        # save the log for debugging
        def target():
            out_log = "ut-profile.log"
            err_log = "ut-profile.err"
            file_out = open(out_log, "w")
            file_err = open(err_log, "w")
            self.process = subprocess.Popen(
                cmd, stdout=file_out, stderr=file_err,
                preexec_fn=os.setsid,
                **kwargs)
            self.stdout, self.stderr = self.process.communicate()
        
        thread = threading.Thread(target=target)
        thread.start()

        thread.join(limit)
        if thread.is_alive():
            killed = True
            # self.process.terminate()
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate()
            thread.join()

        t1 = time.time()
        return {'time': float('inf') if killed else (t1 - t0),
                'timeout': killed,
                'returncode': self.process.returncode,
                'stdout': self.stdout,
                'stderr': self.stderr}
  
  
    def unique(self, api, stage, desired_result):
        """ Get a unique desired result. report result if duplicates """
        assert desired_result != None, "Invalid Desired Result"
        cfg = desired_result.configuration.data
        hashv = self.hash_cfg(api, desired_result)
  
        q = GlobalResult.get(self._glbsession[stage], hashv, cfg)
        if q == None:
            # TODO: fix or remove sql-alchemy
            # Check the pandas dataframes
            arch_path = "../ut-archive.csv"
            if os.path.exists(arch_path):
                keys = [ item[1] for item in self._params[0] ]
                df = pd.read_csv(arch_path)
                check = [ df[k]==v for k, v in cfg.items() ]
                dup = check.pop()
                while len(check) > 0:
                    dup &= check.pop()
                if dup.any():
                    result = Result(time=1) 
                    api.report_result(desired_result, result)
                    return False
            return True
        else:
            result = Result(time=q.result) 
            api.report_result(desired_result, result)
            return False
  
  
    def hash_cfg(self, api, desired_result):
        """ Get the hash value of desired_result """
        cfg = desired_result.configuration.data
        api.manipulator.normalize(cfg)
        hashv = api.manipulator.hash_config(cfg)
        return hashv
  
  
    def training(self, model_list, stage=0):
        """ Initialize ML models with offline data """
        if len(model_list) > 0: 
            self._valid = True
  
            for item in self._params[stage]:
                ptype, pname, prange = item
                if ptype == "EnumParameter": 
                   self._mapping[pname] = dict([(y,x+1) for x,y \
                                               in enumerate(sorted(set(prange)))])
  
            from uptune.plugins import models
            return copy.deepcopy(models.ensemble(model_list, self._mapping))
        return []
  
  
    def multivoting(self, stage, desired_result):
        """ 
        avergaing predicative scores from self.modesl 
        return True if the proposal prediction ranks top 30% over history 
        """
        results = (self._glbsession[stage].query(GlobalResult.result)
                   .order_by(asc(GlobalResult.result)).all()) 
        if len(results) == 0: 
            return True 

        # TODO: decide wether multi-stage use multi-voting or not
        return True
  
        threshold = results[0][0] + self._ratio * (results[-1][0] - results[0][0]) 
        scores = [model.inference(desired_result.configuration.data) 
                      for model in self._models]
        average = sum(scores) / len(scores) 
  
        if average < threshold: return True 
        else: return False
  
    def resume(self):
        # recover the decoded pattern
        arch_path = '../ut-archive.csv'
         
        if os.path.isfile(arch_path):
            print("[ INFO ] Found history records. Trying to re-load the search records...")
            data = pd.read_csv(arch_path)

            # Check if the archive is for this tuning task 
            cols = [ _[1] for _ in self._params[0] ]
            if not set(cols).issubset(set(data.columns)):
                 log.info('archive mismatch. delete archive') 
                 os.system('rm ' + arch_path)
                 return False

            columns = data.columns[1:-1]
            for col in columns:
                if col in self._mapping:
                    cands = [item[-1] for item in self._params[0] if item[1] == col][0]
                    mapping = dict([(i+1, cands[i]) for i in range(len(cands))])
                    data[col].replace(mapping, inplace=True)

            def convert(x):
               try: return int(x) if not "." in x else float(x)
               except: # non-numerical values 
                   try: # convert a perm list
                       x = x.strip('][').split(', ')
                       return [int(_) if not "." in _ else float(_) for _ in x]
                   except: return x

            # Report datas to global database
            for d in data.values: 
                d = d[1:-1]; qor = float(d[-1])
                cfg = dict([(columns[i], convert(d[i])) for i in range(len(self._params[0]))])
                self.global_report(0, 0, self._apis[0], 0, cfg, 'seed', qor)

            self._prev = len(data.values) - 1
            return len(data.values) - 1
  
    def prune(self, api, stage, desired_result):
        """ Prune away duplicate and unpromising proposals """
        if self.unique(api, stage, desired_result) == True:
            # use ML model pruning 
            if self._valid == True:  
                assert len(self._models) > 0, "No model available"
                # generate a weighed score from the model ensemble
                if self.multivoting(stage, desired_result) == True: 
                    return True
                else: 
                    return False 
            # Checking if the dr is being validated
            # TODO: the comparison does not work for object enum
            config = desired_result.configuration.data
            if config in self._pending:
                return False
            self._pending.append(desired_result.configuration.data)
            return True 
        return False

    # Encode enum iterm into an index number
    def encode(self, key, val):
        if key in self._mapping: 
            try:
                return self._mapping[key][val] 
            except:
                print(self._mapping, key, value)
                raise RuntimeError("key error")
        # Permutation type
        elif isinstance(val, list):
            return [val] 
        return val

    # Async task scheduler
    def async_execute(self, template=False):
        self._apis = [self.create_tuning(x, 0, self.create_params()) 
                          for x in range(self._parallel)]
  
        # Create ray actors
        actors = []
        for p in range(self._parallel):
            name = "uptune_actor_p{}".format(p)
            actor = self.cls.options(name=name).remote(p, 0, self.args) 
            actors.append(actor)
  
        # user specified training data + models 
        self._models = self.training(self.args.learning_models) 
  
        # restore history search result
        prev = self.resume()
        start_time = time.time() 
        trial_num = 0
        new_qor_count = 0
        local_results = []
        local_build_times = []

        def get_config(task_list, drs):
            cfgs = dict()
            for index in task_list:
                desired_result = None
                api = self._apis[index]
                while desired_result is None:
                    try: desired_result = api.get_next_desired_result()
                    except: desired_result = None

                # prune and report back to opentuner database 
                while self.prune(api, 0, desired_result) == False:
                    log.warning("duplicate configuration request by %s from node %d", 
                        desired_result.requestor,
                        self._apis.index(api))
                    desired_result = api.get_next_desired_result()

                drs[index] = desired_result
                cfgs[index] = desired_result.configuration.data
            return drs

        # distribute desired results across nodes
        # check the task queue every a few mins
        not_reach_limit = True
        free_task_list = [ _ for _ in range(self._parallel) ]
        keys = [ item[1] for item in self._params[0] ]
        arch_path = "../ut-archive.csv"

        # objects list saves the pending tasks
        objects = list()
        drs = dict()
        while not_reach_limit:
            # Prepare inputs 
            # The new desired result will overwrite the old ones
            drs = get_config(free_task_list, drs)
            if not template: 
                measure_num = trial_num
                if self._prev and trial_num == 0: 
                    measure_num += (self._prev + 1)
                meta = {"UT_MEASURE_NUM": measure_num, 
                        "UT_WORK_DIR":    os.path.abspath("../")}
                self.publish(drs, stage=0, meta=meta)
            # Invoke remote executors
            for index in free_task_list:
                print("[ DEBUG ] dispatch new task on node {}: {}"\
                    .format(index, str(drs[index].configuration.data)))
                obj = actors[index].run.remote(drs[index]) 
                objects.append(obj)
            free_task_list = []

            # List of QoRs returned from the raylet runners
            # Format [ index, {co-variates}, eval_time, QoR ]
            # Check the executor pool periodically
            while True:
                qors, not_ready_refs = ray.wait(objects, timeout=self._interval)
                objects = not_ready_refs
                print("[ DEBUG ] Checking wait time", len(qors), self._interval)
                if (len(qors) > 0):
                    new_qor_count += len(qors)
                    results, covars, eval_times = [], [], []
                    for qor in qors:
                        index, covar_list, eval_time, target = ray.get(qor)
                        print("[ DEBUG ] Free node {}".format(index))
                        free_task_list.append(index)
                        eval_times.append(eval_time)
                        results.append(target)
                        covars.append(covar_list)
                        # local result logging
                        local_results.append(target)
                        local_build_times.append(eval_time)

                    # report and synchronize between apis
                    results = [ Result(time=target) for target in results ]
                    count = 0
                    global_results_sync = dict()
                    for index in free_task_list:
                        api = self._apis[index]
                        dr = drs[index]
                        result = results[count]
                        build_time = eval_times[count]
                        covar = covars[count]

                        api.report_result(dr, result)
                        gr = self.global_report(0, trial_num, api, index, 
                                           dr.configuration.data, 
                                           dr.requestor,
                                           result.time)
                        global_results_sync[index] = gr

                        # Save res for causal dicovery update
                        vals = OrderedDict([(key, self.encode(key, dr.configuration.data[key])) for key in keys]) 
                        elapsed_time = time.time() - start_time

                        # Check whether prev result exist
                        if self._prev and trial_num == 0: 
                            trial_num = trial_num + self._prev + 1
                        is_best = 1 if result.time == self._best[0] else 0
                        df = pd.DataFrame({"time" : elapsed_time, **vals, **covar, 
                                           "build_time" : build_time,
                                           "qor" : result.time, "is_best" : is_best}, 
                                           columns=["time", *keys, *covar.keys(), "build_time", "qor", "is_best"],
                                           index=[trial_num])
                        header = ["time", *keys, *covar.keys(), "build_time", "qor", "is_best"]
                        df.to_csv(arch_path, mode='a', index=False, 
                                  header=False if trial_num > 0 else header)
                        trial_num += 1
                        count += 1
                        
                    # Update the new results to other nodes (apis) 
                    for index, gr in global_results_sync.items():
                        api_count = 0
                        for api in self._apis:
                            if api_count != index: 
                                api.sync([gr])
                            api_count += 1
                    break

            # report local result every self._parallel qors return
            if new_qor_count >= self._parallel:
                new_qor_count = 0
                rets = np.array(local_results)
                eval_times = np.array(local_build_times)

                local_worst = np.nanmax(rets[rets != np.inf])
                local_best  = np.nanmin(rets[rets != np.inf])
                max_build_time = np.nanmax(eval_times[eval_times != np.inf])
                global_best = self._best[0] if self._best else local_best
                if local_best < global_best: global_best = local_best

                print("[ INFO ] {}(#{}/{})".\
                        format(str(datetime.timedelta(seconds=int(elapsed_time))),
                            trial_num, self._limit) + \
                    " - QoR LW({:05.2f})/LB({:05.2f})/GB({:05.2f}) - build time({:05.2f}s)".\
                        format(local_worst, local_best, global_best, max_build_time))
                local_results = []
                local_build_times = []
                
            elapsed_time = time.time() - start_time
            if trial_num > self._limit: 
                print(trail, self._limit)
                not_reach_limit = False
            if elapsed_time > float(self.args.timeout): 
                not_reach_limit = False
                print(elapsed_time)
            if not_reach_limit == False:
                print("[ INFO ] Search ends. Global best {}".format(self._best[0]))

        # End of execution 
        for api in self._apis:
            api.finish()
        return self._cfg        
  
    def main(self, template=False):
        self._apis = [self.create_tuning(x, 0, self.create_params()) 
                          for x in range(self._parallel)]
        # Create ray actors
        actors = []
        for p in range(self._parallel):
            name = "uptune_actor_p{}".format(p)
            actor = self.cls.options(name=name).remote(p, 0, self.args) 
            actors.append(actor)
  
        # user specified training data + models 
        self._models = self.training(self.args.learning_models) 
  
        # restore history search result
        prev = self.resume()
        start_time = time.time() 

        # the main searching loop
        for epoch in range(self._limit):
            drs, cfgs = list(), list()
            for api in self._apis:
                desired_result = None

                while desired_result is None:
                    try: desired_result = api.get_next_desired_result()
                    except: desired_result = None
  
                # prune and report back to opentuner database 
                while self.prune(api, 0, desired_result) == False:
                    log.warning("duplicate configuration request by %s from node %d", 
                        desired_result.requestor,
                        self._apis.index(api))
                    desired_result = api.get_next_desired_result()
  
                drs.append(desired_result)
                cfgs.append(desired_result.configuration.data)
            
            # assert and run in parallel with ray remote
            # truncate = lambda x: x + "..." if len(x) > 75 else x
            assert len(cfgs) == self._parallel, \
                "All available cfgs have been explored"

            # distribute desired results across nodes
            base = epoch * self._parallel 
            if not template: 
                measure_num = base
                if self._prev: measure_num += (self._prev + 1)
                meta = {"UT_MEASURE_NUM": measure_num, 
                        "UT_WORK_DIR":    os.path.abspath("../")}
                self.publish(drs, stage=0, meta=meta)
            objects = [ actor.run.remote(drs[actors.index(actor)]) 
                          for actor in actors ]

            # List of QoRs returned from the raylet runners
            # Format [ index, {co-variates}, eval_time, QoR ]
            # qors = ray.get(objects, timeout=self.args.runtime_limit+10)

            # Check the executor pool periodically (5 mins)
            interval = 5 * 60
            qors, not_ready_refs = ray.wait(objects, 
                num_returns=self._parallel, timeout=self.args.runtime_limit)

            # Dispatch the tasks asynchronously 
            results, covars, eval_times = [], [], []
            for index in range(len(objects)):
                item = objects[index]
                if item in qors:
                    index, covar_list, eval_time, target = ray.get(item)
                    eval_times.append(eval_time)
                    results.append(target)
                    covars.append(covar_list)
                
                # Cancel timeed-out tasks
                else:
                    assert item in not_ready_refs, "Not found object ref"
                    # Kill the dead actor and create a new actor 
                    print("[ WARNING ] Thread #{} timed-out. Creating new actor...".format(index))
                    del actors[index]
                    new_actor = self.cls.remote(index, 0, self.args) 
                    actors.insert(index, new_actor)

                    eval_times.append(float("inf"))
                    results.append(float("inf"))
                    covars.append({})                   
  
            elapsed_time = time.time() - start_time
            arch_path = "../ut-archive.csv"

            rets = np.array(results)
            eval_times = np.array(eval_times)

            local_worst = np.nanmax(rets[rets != np.inf])
            local_best  = np.nanmin(rets[rets != np.inf])
            max_build_time = np.nanmax(eval_times[eval_times != np.inf])
            global_best = self._best[0] if self._best else local_best
            if local_best < global_best: global_best = local_best
            
            print("[ INFO ] {}(#{}/{})".\
                    format(str(datetime.timedelta(seconds=int(elapsed_time))),
                        epoch * self._parallel, self._limit) + \
                " - QoR LW({:05.2f})/LB({:05.2f})/GB({:05.2f}) - build time({:05.2f}s)".\
                    format(local_worst, local_best, global_best, max_build_time))

            keys = [ item[1] for item in self._params[0] ]
            results = [ Result(time=target) for target in results ]

            for api, dr, covar, build_time, result \
                in zip(self._apis, drs, covars, eval_times, results):
                api.report_result(dr, result)
                self.global_report(0, epoch, api,
                                   self._apis.index(api), 
                                   dr.configuration.data, 
                                   dr.requestor,
                                   result.time)

                # Save res for causal dicovery update
                index = base + drs.index(dr)
                vals = OrderedDict([(key, self.encode(key, dr.configuration.data[key])) for key in keys]) 
                
                # Check whether prev result exist
                if self._prev: index = index + self._prev + 1
                is_best = 1 if result.time == self._best[0] else 0
                df = pd.DataFrame({"time" : elapsed_time, **vals, **covar, 
                                   "build_time" : build_time,
                                   "qor" : result.time, "is_best" : is_best}, 
                                   columns=["time", *keys, *covar.keys(), "build_time", "qor", "is_best"],
                                   index=[index])
                header = ["time", *keys, *covar.keys(), "build_time", "qor", "is_best"]
                df.to_csv(arch_path, mode='a', index=False, 
                          header=False if index > 0 else header)
  
            for api in self._apis: # sync across nodes
                self.synchronize(0, api, self._apis.index(api), epoch)

            # update causal baysien graph 
            # if epoch % 10 == 0:
            #     data = pd.read_csv('../archive.csv')
            #     data = (data-data.mean())/data.std()
            #     print(notears(data.values[:, 2:-1]))

            # time check and plot diagram
            if elapsed_time > float(self.args.timeout): 
                log.info('%s runtime exceeds timeout %ds. global_best is %f', 
                             str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                             int(elapsed_time), self._best[0])
                break 
  
        # End of execution 
        for api in self._apis:
            api.finish()

        log.info('%s tuning complete. global_best is %f', 
                     str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                     self._best[0] if self._best else float('inf'))
        return self._cfg


    # Fine-grained auto-tuning control 
    def set_actor_cls(self, actor):
        """ set actor cls from builder and tmpl """
        self.cls = actor

    def create_instances(self):
        """ create single-stage api controller, ray actors and ML model instances"""
        self._actors = [self.cls.remote(_, 0, self.args) 
                            for _ in range(self._parallel)]
        self._apis = [self.create_tuning(x, 0, self.create_params()) 
                          for x in range(self._parallel)]
        self._models = self.training(self.args.learning_models) 

    def finish_tuning(self):
        """ return best cfg """
        best_cfgs = [api.get_best_configuration() for api in self._apis]
        for api in self._apis:
            try: api.finish()
            except: pass
        return best_cfgs
        
    def generate_dr(self): 
        """ singe-stage generate desired result """
        drs, idxs = list(), list()
        for api in self._apis:
            desired_result = api.get_next_desired_result()
            if desired_result is None:
                continue
  
            while self.prune(api, 0, desired_result) == False:
                log.warning("duplicate configuration request by %s from node %d", 
                    desired_result.requestor,
                    self._apis.index(api))
                desired_result = api.get_next_desired_result()
  
            drs.append(desired_result)
            idxs.append(self._apis.index(api))

        assert len(drs) == self._parallel, \
               "All available cfgs have been explored"
        return drs, idxs 

    def rpt_and_sync(self, epoch, drs, results, mapping=None, stage=0):
        """ report and synchronize result """
        log.info('Global best qor %f', 
            self._best[stage] if self._best is not None else float('inf'))

        idxs = tuple([mapping[_] for _ in drs]) if mapping else None
        apis = [self._apis[i] for i in idxs] if idxs else self._apis  
        for api, dr, result in zip(apis, drs, results):
            api.report_result(dr, result)
            self.global_report(stage,
                               epoch, 
                               api,
                               self._apis.index(api), 
                               dr.configuration.data, 
                               dr.requestor,
                               result.time)
  
        for api in self._apis:
            self.synchronize(stage, api, self._apis.index(api), epoch)

class RunProgram(object):
    """
    Ray Actor to be called by object of ParallelTuning Class
    Extending dataflow from functional programming  
    Reference: https://ray.readthedocs.io/en/latest/actors.html
    """
    def __init__(self, index, stage, args=None):
        self.index       = index             
        self.stage       = stage
        self.args        = args
        self.workpath    = None
        self.process     = None
        self.stdout      = str()
        self.stderr      = str()
        self.dumper      = JinjaParser() 

    # Used before lauching tuning task through raylet
    def start_run(self, nodes=1):

        # Running tuning tasks in a single-node machine
        # when running across multiple compute nodes (not sharing the same FS)
        # search instances need to find available nodes 
        if nodes == 1: 
            self.workpath = str(self.index)
            dir_in_use = self.workpath + '-inuse'
            if not os.path.isdir(dir_in_use):
                os.rename(self.workpath, dir_in_use)
            os.chdir(self.workpath + '-inuse')

        else: 
            for folder in next(os.walk('.'))[1]:
                if folder.isdigit():
                    self.workpath = folder
                    os.rename(folder, folder + '-inuse')
                    os.chdir(folder + '-inuse')
                    break

    def end_run(self):
        os.chdir("../")
        os.rename(self.workpath + '-inuse', self.workpath)

    def call_program(self, cmd, aws=False, sample=False, 
                     limit=None, memory_limit=None):
        kwargs = dict()
        subenv = os.environ.copy()
        subenv["UT_TUNE_START"] = "True"
        subenv["UT_CURR_INDEX"] = str(self.index)
        subenv["UT_CURR_STAGE"] = str(self.stage)

        # early exit in multistage & aws
        if sample: subenv["UT_MULTI_STAGE_SAMPLE"] = "True"
        if aws: subenv["UT_AWS_S3_BUCKET"] = "True"

        if limit is float('inf'):
            limit = None
        if type(cmd) in (str, str):
            kwargs['shell'] = True
            kwargs['env'] = subenv

        killed = False
        t0 = time.time()

        def target():
            out_log = "out_stage{}-index{}.log".format(self.stage, self.index)
            err_log = "err_stage{}-index{}.log".format(self.stage, self.index)
            file_out = open(out_log, "w")
            file_err = open(err_log, "w")
            self.process = subprocess.Popen(
                cmd, stdout=file_out, stderr=file_err,
                preexec_fn=os.setsid,
                **kwargs)
            self.stdout, self.stderr = self.process.communicate()
        
        thread = threading.Thread(target=target)
        thread.start()

        thread.join(limit)
        if thread.is_alive():
            killed = True
            # self.process.terminate()
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate()
            thread.join()

        t1 = time.time()
        return {'time': float('inf') if killed else (t1 - t0),
                'timeout': killed,
                'returncode': self.process.returncode,
                'stdout': self.stdout,
                'stderr': self.stderr}

    def run(self, dr):
        raise RuntimeError("ParallelTuning.run() not implemented")
    

# Expr for Functional Module Reuse
class ProgramTune(ParallelTuning):
    def __init__(self, cls, args, *pargs, **kwargs):
        super(ProgramTune, self).__init__(cls, args, *pargs, **kwargs)
        self.before_run()


@ray.remote
class SingleProcess(RunProgram): 
    def run(self, dr):
        import random
        os.system('sleep 10')
        return random.randint(0, 10)

if __name__ == '__main__':
    argparser = uptune.default_argparser()
    pt = ProgramTune(SingleProcess, argparser.parse_args())
    pt.main()
  
