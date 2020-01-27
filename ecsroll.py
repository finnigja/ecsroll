#!/usr/bin/env python3

import argparse
import boto3
import sys
import tabulate
from time import sleep

PROVIDER_PROFILE = 'profile'
PROVIDER_ENV = 'env'

DEFAULT_PROVIDER = PROVIDER_PROFILE
DEFAULT_PROFILE = 'default'  # name of awscli / boto3 profile to target
DEFAULT_CLUSTER = 'test-ecs-cluster'  # name of ECS cluster to target
DEFAULT_ACTION = 'replace'  # 'reboot' or 'replace'
DEFAULT_WAIT = 30

WAIT_TIME = DEFAULT_WAIT

AUTO_YES = False

INSTANCE_FIELDS = ['ec2InstanceId', 'containerInstanceArn', 'status', 'runningTasksCount', 'pendingTasksCount']


def yes_or_exit(message):
    if not AUTO_YES:
        choices = ['y', 'n']
        choice = ''
        while choice not in choices:
            sys.stdout.write('{0} {1} '.format(message, '/'.join(choices)))
            choice = input().lower()
        if choice != 'y':
            print('Exiting... please review output, and take any manual steps needed to normalize enviroment.')
            sys.exit(2)
    sys.stdout.write('\n')


def countdown(msg, t):
    print('{}...'.format(msg))
    while t:
        mins, secs = divmod(t, 60)
        timeformat = '{:02d}:{:02d}'.format(int(mins), int(secs))
        print(timeformat, end='\r')
        sleep(1)
        t -= 1


def cluster_exists(ecs_client, target_cluster):
    clusters = ecs_client.list_clusters()
    for cluster in clusters['clusterArns']:
        if cluster.split('/')[1] == target_cluster:
            return True
    return False


def get_cluster_instances(ecs_client, cluster):
    # create list of all instances in cluster
    container_instances = []
    paginator = ecs_client.get_paginator('list_container_instances')
    page_iterator = paginator.paginate(cluster=cluster)
    for page in page_iterator:
        for arn in page['containerInstanceArns']:
            container_instances.append(arn)
    cluster_instances = []
    # collect details on each container instance
    for arn in container_instances:
        desc = ecs_client.describe_container_instances(
            cluster=cluster,
            containerInstances=[arn, ]
        )
        if len(desc.get('containerInstances')) > 0:
            detail = desc['containerInstances'][0]
            cluster_instances.append([detail[field] for field in INSTANCE_FIELDS])
    return cluster_instances


def print_cluster_instances(instances):
    print('{0}\n'.format(tabulate.tabulate(instances, headers=INSTANCE_FIELDS)))


def get_autoscaling_groups(as_client, instances):
    asgs = set()
    paginator = as_client.get_paginator('describe_auto_scaling_instances')
    page_iterator = paginator.paginate()
    for page in page_iterator:
        for asi in page['AutoScalingInstances']:
            if asi['InstanceId'] in [i[0] for i in instances]:
                asgs.add(asi['AutoScalingGroupName'])
    return list(asgs)


def bump_autoscaling_group(as_client, asg, hop):
    asg_obj = as_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg, ])['AutoScalingGroups'][0]
    as_client.update_auto_scaling_group(
        AutoScalingGroupName=asg,
        MinSize=asg_obj['MinSize'] + hop,
        MaxSize=asg_obj['MaxSize'] + hop,
        DesiredCapacity=asg_obj['DesiredCapacity'] + hop
    )


def set_scalein_protection_for_instances(as_client, asg, cluster_instances, protection):
    instance_ids = []
    for instance in cluster_instances:
        instance_ids.append(instance[INSTANCE_FIELDS.index('ec2InstanceId')])
    print('Setting scale-in protection \'{}\' for instances ({})'.format(
        protection, ', '.join(instance_ids)
    ))
    as_client.set_instance_protection(
        AutoScalingGroupName=asg, InstanceIds=instance_ids, ProtectedFromScaleIn=protection
    )


def wait_until_instance_count(ecs_client, target_cluster, count, seconds=WAIT_TIME):
    countdown('Waiting for cluster size change (expected instance count: {})'.format(count), seconds*3)
    current_instances = get_cluster_instances(ecs_client, target_cluster)
    if len(current_instances) != count:
        yes_or_exit('There are currently {} instances, but expecting {} - keep waiting?'.format(
            len(current_instances), count
        ))
        wait_until_instance_count(ecs_client, target_cluster, count)


def wait_until_instance_status(ecs_client, target_cluster, instance_id, status):
    countdown('Waiting for instance {} to have {} status'.format(instance_id, status), WAIT_TIME/2)
    current_instances = get_cluster_instances(ecs_client, target_cluster)
    for instance in current_instances:
        if instance[INSTANCE_FIELDS.index('ec2InstanceId')] == instance_id:
            if instance[INSTANCE_FIELDS.index('status')] == status:
                return
            else:
                yes_or_exit('Instance {} has status {} but expecting {} - keep waiting?'.format(
                    instance_id, instance[INSTANCE_FIELDS.index('status')], status
                ))
                wait_until_instance_status(ecs_client, target_cluster, instance_id, status)
            break
    print('ERROR: wait_until_instance_status cannot find passed instance: {}'.format(instance_id))
    sys.exit(2)


def get_overflow_instance_ids(original_instances, current_instances):
    overflow_ids = []
    original_ec2_ids = [i[INSTANCE_FIELDS.index('ec2InstanceId')] for i in original_instances]
    current_ec2_ids = [i[INSTANCE_FIELDS.index('ec2InstanceId')] for i in current_instances]
    for i, ec2_id in enumerate(current_ec2_ids):
        if ec2_id not in original_ec2_ids:
            overflow_ids.append(
                {
                    'ec2': current_instances[i][INSTANCE_FIELDS.index('ec2InstanceId')],
                    'ecs': current_instances[i][INSTANCE_FIELDS.index('containerInstanceArn')],
                }
            )
    return overflow_ids


def activate_instance(ecs_client, target_cluster, instance_id):
    ecs_client.update_container_instances_state(
        cluster=target_cluster, containerInstances=[instance_id, ], status='ACTIVE'
    )


def wait_until_instance_drained(ecs_client, target_cluster, instance_id):
    print('Marking ECS instance {} as DRAINING'.format(instance_id))
    ecs_client.update_container_instances_state(
        cluster=target_cluster, containerInstances=[instance_id, ], status='DRAINING'
    )

    drained = False
    while not drained:
        response = ecs_client.list_tasks(cluster=target_cluster, containerInstance=instance_id, desiredStatus='RUNNING')
        running = len(response['taskArns'])
        drained = (running == 0)
        if not drained:
            countdown('Waiting for instance {} to drain; currently running {} tasks'.format(instance_id, running), WAIT_TIME)


def wait_until_instance_ec2_ok(ec2_client, ec2_instance_id):
    ok = False
    while not ok:
        response = ec2_client.describe_instance_status(InstanceIds=[ec2_instance_id])
        status = response['InstanceStatuses'][0]['InstanceStatus']['Status']
        ok = (status == 'ok')
        if not ok:
            countdown('Waiting for instance {} to be \'ok\'; currently \'{}\''.format(ec2_instance_id, status), WAIT_TIME)


def wait_until_instance_ecs_connected(ecs_client, ecs_instance_id, target_cluster):
    connected = False
    while not connected:
        response = ecs_client.describe_container_instances(cluster=target_cluster, containerInstances=[ecs_instance_id])
        connected = response['containerInstances'][0]['agentConnected']
        ec2_instance_id = response['containerInstances'][0]['ec2InstanceId']
        if not connected:
            countdown('Waiting for instance {} to have ECS agent connected'.format(ec2_instance_id), 60)


def setup_for_roll(profile, target_cluster):
    if args.provider == PROVIDER_PROFILE:
        yes_or_exit('Continue, working with AWS profile \'{}\'?'.format(profile))
        session = boto3.Session(profile_name=profile)
    else:
        session = boto3.Session()
    ecs_client = session.client('ecs')
    ec2_client = session.client('ec2')
    as_client = session.client('autoscaling')
    if not cluster_exists(ecs_client, target_cluster):
        print('ERROR: ECS cluster \'{}\' does not exist in targeted AWS environment'.format(
            target_cluster
        ))
        sys.exit(2)

    cluster_instances = get_cluster_instances(ecs_client, target_cluster)
    asgs = get_autoscaling_groups(as_client, cluster_instances)
    if len(asgs) != 1:
        print('ERROR: EC2 instances associated with ECS cluster are not associated with a single ASG.')
        print('\tECS Cluster: {}'.format(target_cluster))
        print('\tEC2 Instances: {}'.format(', '.join(cluster_instances)))
        print('\tASG [{}]: {}'.format(len(asgs), ', '.join(asgs)))
        sys.exit(2)
    asg = asgs[0]

    yes_or_exit('Continue, working with ECS cluster \'{}\'?'.format(target_cluster))
    yes_or_exit('Continue, working with ASG \'{}\'?'.format(asg))
    print_cluster_instances(cluster_instances)

    return (ecs_client, ec2_client, as_client, cluster_instances, asg)


def get_new_instance(original, replacement, current):
    original = [[
        i[INSTANCE_FIELDS.index('ec2InstanceId')], i[INSTANCE_FIELDS.index('containerInstanceArn')]
    ] for i in original]
    replacement = [[
        i[INSTANCE_FIELDS.index('ec2InstanceId')], i[INSTANCE_FIELDS.index('containerInstanceArn')]
    ] for i in replacement]
    current = [[
        i[INSTANCE_FIELDS.index('ec2InstanceId')], i[INSTANCE_FIELDS.index('containerInstanceArn')]
    ] for i in current]
    for instance in current:
        if instance not in original and instance not in replacement:
            return instance


def do_cluster_replace(profile, target_cluster):
    ecs_client, ec2_client, as_client, cluster_instances, asg = setup_for_roll(profile, target_cluster)
    yes_or_exit('Initiate REPLACE cycle for {} ECS instances ({})?'.format(
        len(cluster_instances), ', '.join([i[0] for i in cluster_instances])
    ))
    print('Increasing ASG size by 1 to maintain cluster capacity during rolling replace')
    bump_autoscaling_group(as_client, asg, 1)
    replacement_instances = []

    for i, instance in enumerate(cluster_instances):  # for all original instances
        ec2_instance_id = instance[INSTANCE_FIELDS.index('ec2InstanceId')]
        ecs_instance_id = instance[INSTANCE_FIELDS.index('containerInstanceArn')]
        yes_or_exit('\nPerform replace {} of {}, targeting instance {} [{}]?'.format(
            i+1, len(cluster_instances), ec2_instance_id, ecs_instance_id
        ))

        countdown('Waiting for ASG to rightsize ECS cluster', WAIT_TIME)
        wait_until_instance_count(ecs_client, target_cluster, len(cluster_instances) + 1)
        new_instance = get_new_instance(
            cluster_instances, replacement_instances, get_cluster_instances(ecs_client, target_cluster)
        )
        new_ec2_instance_id = new_instance[INSTANCE_FIELDS.index('ec2InstanceId')]
        new_ecs_instance_id = new_instance[INSTANCE_FIELDS.index('containerInstanceArn')]
        wait_until_instance_ec2_ok(ec2_client, new_ec2_instance_id)
        wait_until_instance_ecs_connected(ecs_client, new_ecs_instance_id, target_cluster)
        print('New instance {} [{}] is up and joined to ECS cluster.'.format(new_ec2_instance_id, new_ecs_instance_id))
        replacement_instances.append(new_instance)

        print('Current cluster members:')
        print_cluster_instances(get_cluster_instances(ecs_client, target_cluster))

        yes_or_exit('\nDrain and terminate original instance {}/{} {} [{}]?'.format(
            i+1, len(cluster_instances), ec2_instance_id, ecs_instance_id
        ))

        wait_until_instance_drained(ecs_client, target_cluster, ecs_instance_id)

        if i < (len(cluster_instances) - 1):
            #  terminate an original instance
            ec2_client.terminate_instances(InstanceIds=[ec2_instance_id, ])
            countdown('Terminating original instance {} [{}]'.format(ec2_instance_id, ecs_instance_id), WAIT_TIME)
        else:
            # for the final instance, just downsize cluster & let AS / ECS handle it
            set_scalein_protection_for_instances(as_client, asg, replacement_instances, True)
            bump_autoscaling_group(as_client, asg, -1)
            countdown('Returned to original ASG size, waiting for ASG to downsize ECS cluster', WAIT_TIME*2)
            wait_until_instance_count(ecs_client, target_cluster, len(cluster_instances))
            set_scalein_protection_for_instances(as_client, asg, replacement_instances, False)

    # .. and we're done
    print('ECS cluster has been returned to original size. Current cluster members:')
    print_cluster_instances(get_cluster_instances(ecs_client, target_cluster))


def do_cluster_reboot(profile, target_cluster):
    ecs_client, ec2_client, as_client, cluster_instances, asg = setup_for_roll(profile, target_cluster)
    yes_or_exit('Initiate REBOOT cycle for {} ECS instances ({})?'.format(
        len(cluster_instances), ', '.join([i[0] for i in cluster_instances])
    ))

    print('Increasing ASG size by 1 to maintain cluster capacity during rolling reboot')
    bump_autoscaling_group(as_client, asg, 1)
    countdown('Waiting for ASG to upsize ECS cluster', WAIT_TIME)
    # wait until the additional instance joins the cluster
    wait_until_instance_count(ecs_client, target_cluster, len(cluster_instances) + 1)

    print('ECS cluster now has the expected number of instances:')
    print_cluster_instances(get_cluster_instances(ecs_client, target_cluster))
    yes_or_exit('Capacity has been increased; perform rolling reboot of original instances?')
    for i, instance in enumerate(cluster_instances):  # for all original instances
        ec2_instance_id = instance[INSTANCE_FIELDS.index('ec2InstanceId')]
        ecs_instance_id = instance[INSTANCE_FIELDS.index('containerInstanceArn')]
        yes_or_exit('\nPerform reboot {} of {}, targeting instance {} [{}]?'.format(
            i+1, len(cluster_instances), ec2_instance_id, ecs_instance_id
        ))
        #  drain instance
        wait_until_instance_drained(ecs_client, target_cluster, ecs_instance_id)
        # 1st reboot instance (this picks up any unapplied security updates when it boots)
        ec2_client.reboot_instances(InstanceIds=[ec2_instance_id, ])
        countdown('Reboot (1/2) for instance {} [{}]'.format(ec2_instance_id, ecs_instance_id), WAIT_TIME)
        wait_until_instance_ec2_ok(ec2_client, ec2_instance_id)
        wait_until_instance_ecs_connected(ecs_client, ecs_instance_id, target_cluster)
        # 2nd reboot of instance (boots to new kernel, if it was updated)
        ec2_client.reboot_instances(InstanceIds=[ec2_instance_id, ])
        countdown('Reboot (2/2) for instance {} [{}]'.format(ec2_instance_id, ecs_instance_id), WAIT_TIME)
        wait_until_instance_ec2_ok(ec2_client, ec2_instance_id)
        wait_until_instance_ecs_connected(ecs_client, ecs_instance_id, target_cluster)
        #  mark as ACTIVE and verify that it is
        print('Marking ECS instance {} as ACTIVE'.format(ecs_instance_id))
        ecs_client.update_container_instances_state(
            cluster=target_cluster, containerInstances=[ecs_instance_id, ], status='ACTIVE'
        )
        wait_until_instance_status(ecs_client, target_cluster, ec2_instance_id, 'ACTIVE')
        print('Current state of cluster:')
        print_cluster_instances(get_cluster_instances(ecs_client, target_cluster))

    yes_or_exit('Reboots completed; return cluster to original size by draining and terminating overflow instance?')

    # drain overflow instance
    overflow_ids = get_overflow_instance_ids(cluster_instances, get_cluster_instances(ecs_client, target_cluster))
    if len(overflow_ids) != 1:
        print('ERROR: Unexpected number of overflow instances ({})'.format(', '.join(
            [oid['ec2'] for oid in overflow_ids]
        )))
        print('       Exiting, manual cleanup likely needed')
    wait_until_instance_drained(ecs_client, target_cluster, overflow_ids[0]['ecs'])

    # downsize cluster & wait until overflow instance is gone
    set_scalein_protection_for_instances(as_client, asg, cluster_instances, True)
    bump_autoscaling_group(as_client, asg, -1)
    countdown('Returned to original ASG size, waiting for ASG to downsize ECS cluster', WAIT_TIME)
    wait_until_instance_count(ecs_client, target_cluster, len(cluster_instances))
    set_scalein_protection_for_instances(as_client, asg, cluster_instances, False)

    # .. and we're done
    print('ECS cluster has been returned to original size:')
    print_cluster_instances(get_cluster_instances(ecs_client, target_cluster))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='ecsroll', description='AWS ECS Maintenance Script')
    parser.add_argument(
        '--cluster', '-c', nargs='?', default=DEFAULT_CLUSTER,
        help='Name of ECS cluster to maintain (default: \'{0}\')'.format(DEFAULT_CLUSTER)
    )
    parser.add_argument(
        '--profile', '-p', nargs='?', default=DEFAULT_PROFILE,
        help='Name of AWS profile to target (default: \'{0}\')'.format(DEFAULT_PROFILE)
    )
    parser.add_argument(
        '--wait', '-w', nargs='?', default=DEFAULT_WAIT, type=int,
        help='Base for timer to wait between actions (default: \'{0}\')'.format(DEFAULT_WAIT)
    )
    parser.add_argument(
        '--provider', '-r', nargs='?', default=DEFAULT_PROVIDER, choices=[PROVIDER_PROFILE, PROVIDER_ENV],
        help='AWS credential provider method to use (default: \'{0}\', choose from [\'{1}\',\'{2}\'])'.format(
            PROVIDER_PROFILE, PROVIDER_PROFILE, PROVIDER_ENV)
    )
    parser.add_argument(
        '--yes', '-y', default=AUTO_YES, action='store_true',
        help='Answers \'yes\' to all prompts'
    )
    parser.add_argument(
        'action', nargs='?', default=DEFAULT_ACTION,
        help='Action to take (default: \'{0}\')'.format(DEFAULT_ACTION)
    )
    args = parser.parse_args()

    WAIT_TIME = args.wait
    AUTO_YES = args.yes

    if args.provider == PROVIDER_PROFILE:
        session = boto3.Session()
        if args.profile not in session.available_profiles:
            print('ERROR: AWS profile \'{0}\' not configured.'.format(args.profile))
            print('       Available AWS profiles: {0}'.format(', '.join(session.available_profiles)))
            sys.exit(2)
        print('Using AWS profile \'{0}\''.format(args.profile))
    print('Initiating \'{1}\' maintenance for ECS cluster \'{0}\'...'.format(
        args.cluster, args.action.upper()
    ))

    if args.action.lower() == 'reboot':
        do_cluster_reboot(args.profile, args.cluster)
    elif args.action.lower() == 'replace':
        do_cluster_replace(args.profile, args.cluster)
    else:
        print('ERROR: Don\'t know what to do with action \'{}\'.'.format(args.action))
