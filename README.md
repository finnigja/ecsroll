# ecsroll

Interactive Python/boto3 script for rotating/rebooting EC2 instances in an ECS cluster.

# Overview

ecsroll uses a combination of AWS EC2 & ECS functionality to execute one of two actions:

* 'replace': rotate an ECS cluster to all-new EC2 instances
* 'reboot': reboot the EC2 instances making up an ECS cluster

The basic mechanism:
* Adds an extra 'overflow' instance to the target ECS cluster, using EC2 ASG.
* Uses ECS [container instance draining](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/container-instance-draining.html) to cycle instances out of the cluster and EC2 APIs to perform necessary instance changes/maintenance.
* Returns the ASG to its original size, after applying scale-in protection to necessary instances.

YMMV, but if you combine this with Amazon Linux, which installs available patches at boot time, then it's a straightforward way to update your EC2 instances without downtime.

Recently I've started using [needs-restarting](https://chair6.net/amazon-linux-security-updates-needs-restarting.html), which reduced the steps required for the 'replace' action and would also allow the 'reboot' steps in this script to be simplified.

The script is currently quite interactive (presents a y/n for each instance is works on) by default, but has CLI flags for a more unattended experience. See the usage below.

# Examples

```
python ecsroll.py replace --cluster test-ecs-cluster -w 10 # Replace instances in `test-ecs-cluster`, use 10s as the base action timer (to have a slightly quicker feedback loop, but potentially more prompts)
```

```
python ecsroll.py reboot --cluster test-ecs-cluster -r env -y # Reboot cluster `test-ecs-cluster`, use AWS credentials from Environment variables and automatically respond yes to any prompts
```

# Usage
```
$ python ecsroll.py -h
usage: ecsroll [-h] [--cluster [CLUSTER]] [--profile [PROFILE]]
               [--wait [WAIT]] [--provider [{profile,env}]] [--yes]
               [action]

AWS ECS Maintenance Script

positional arguments:
  action                Action to take (default: 'replace')

optional arguments:
  -h, --help            show this help message and exit
  --cluster [CLUSTER], -c [CLUSTER]
                        Name of ECS cluster to maintain (default: 'test-ecs-cluster')
  --profile [PROFILE], -p [PROFILE]
                        Name of AWS profile to target (default: 'default')
  --wait [WAIT], -w [WAIT]
                        Base for timer to wait between actions (default: '30')
  --provider [{profile,env}], -r [{profile,env}]
                        AWS credential provider method to use (default:
                        'profile', choose from ['profile','env'])
  --yes, -y             Answers 'yes' to all prompts
```

# Author

https://twitter.com/chair6
