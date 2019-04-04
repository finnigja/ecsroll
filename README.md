# ecsroll

Interactive Python/boto3 script for rotating/rebooting EC2 instances in an ECS cluster.

# Overview

Uses a combination of AWS EC2 & ECS functionality to either rotate an ECS cluster to all-new instances, or reboot the instances making up a cluster.

If you combine this with Amazon Linux, which installs available patches at boot time, then it's a straightforward way to update your EC2 instances without downtime.

# Author

https://twitter.com/chair6
