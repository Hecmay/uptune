#! /usr/bin/env python
import sys
import os
import cmd
import uptune
import datetime
from uptune.src.codegen import codegen

def main():
    if not sys.argv[1:] or sys.argv[1] in ("--help", "-h"):
        print("UpTune  ")
        print("Usage: \"python -m uptune.on script [--args] ")
        sys.exit(2)

    mainpyfile =  sys.argv[2] if sys.argv[1] == 'python' else sys.argv[1]
    if not os.path.exists(mainpyfile):
        print('Error:', mainpyfile, 'does not exist')
        sys.exit(1)

    # Remove uptune.on from argument list
    del sys.argv[0] 
    sys.path[0] = os.path.dirname(mainpyfile) 

    # Extract running commands from input arguments
    argparser = uptune.default_argparser()
    args, unknown = argparser.parse_known_args()
    sys.argv = sys.argv[:1] + unknown
    command = " ".join(sys.argv)
 
    # Initialize controller and launch the tuning tasks
    workdir = os.path.abspath(os.getcwd())
    os.environ["UT_WORK_DIR"] = workdir
    print("[ INFO ] Uptune work directory {}".format(workdir))
    controller, mode, tpl = codegen(mainpyfile, args, command)
    tune_mode = "intrusive" if not tpl else "directive"
    config_str = "threads({}), test-limit({}), timeout({}), build-timeout({})".\
        format(args.parallel_factor, args.test_limit, 
               str(datetime.timedelta(seconds=args.timeout)),
               str(datetime.timedelta(seconds=args.runtime_limit)))

    if mode == "single":   
        print("[ INFO ] Single-stage({}), {}".format(tune_mode, config_str)) 
        controller.async_execute(template=tpl)

    elif mode == "multi-stage":    
        print("[ INFO ] Multi-stage({}), {}".format(tune_mode, config_str)) 
        controller.multirun(template=tpl)

    elif mode == "decouple": 
        assert tune_mode == "intrusive"
        print("[ INFO ] Auto-decoupling({}), {}".format(tune_mode, config_str)) 
        controller.decouple()
    
if __name__ == '__main__':
    main()
    
    
