"""An AWS Python Pulumi program"""

from pathlib import Path
from requests import get as r_get
import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx

internal_ip_range = "10.230.0.0/16"
vpc = awsx.ec2.Vpc(
    "vpc",
    cidr_block=internal_ip_range,
    enable_dns_hostnames=True,
    enable_dns_support=True,
    nat_gateways=awsx.ec2.NatGatewayConfigurationArgs(
        strategy=awsx.ec2.NatGatewayStrategy.SINGLE,
    ),
)

aws_region = aws.get_region()

# talos_version = r_get(
#     f"https://api.github.com/repos/siderolabs/talos/releases/latest"
# ).json()["tag_name"]
# hardcoding for now, since 1.7.5 seems to be having issues
# talos_version = "v1.7.4"
# talos_version = "v1.6.8"
talos_version = "v1.7.0"

ami_id_array: list[dict[str, str]] = r_get(
    # from: youtu.be/Erlfg6VhfJE
    f"https://github.com/siderolabs/talos/releases/download/{talos_version}/cloud-images.json"
).json()

for ami_id in ami_id_array:
    if ami_id["cloud"] == "aws":
        if ami_id["region"] == aws_region.name:
            if ami_id["arch"] == "amd64":
                talos_ami_id = ami_id["id"]
                break

talos_ami = aws.ec2.Ami.get("talos-ami", id=talos_ami_id)

talos_sg = aws.ec2.SecurityGroup(
    "talosSg",
    vpc_id=vpc.vpc_id,
    description="Security group  the Talos cluster",
)

# allow from other talos nodes
aws.vpc.SecurityGroupIngressRule(
    "allowAllTalosSg",
    security_group_id=talos_sg.id,
    referenced_security_group_id=talos_sg.id,
    ip_protocol="-1",
    from_port=0,
    to_port=65535,
)

current_ip = r_get("https://api.ipify.org/")
# allow from my IP address
aws.vpc.SecurityGroupIngressRule(
    "allowFromMyIp",
    security_group_id=talos_sg.id,
    cidr_ipv4=f"{current_ip.text}/32",
    ip_protocol="-1",
    from_port=0,
    to_port=65535,
)

# allow all outbound
aws.vpc.SecurityGroupEgressRule(
    "allowAllOutbound",
    security_group_id=talos_sg.id,
    ip_protocol="-1",
    from_port=0,
    to_port=65535,
    cidr_ipv4="0.0.0.0/0",
)

talos_lb_sg = aws.ec2.SecurityGroup(
    "talosLbSg",
    vpc_id=vpc.vpc_id,
    description="Security group for the Talos load balancer",
)
# allow k8s api inbound to load balancer
aws.vpc.SecurityGroupIngressRule(
    "allowK8sApi",
    security_group_id=talos_lb_sg.id,
    cidr_ipv4="0.0.0.0/0",
    ip_protocol="tcp",
    from_port=6443,
    to_port=6443,
)
# allow k8s api traffic outbound to nodes
aws.vpc.SecurityGroupEgressRule(
    "allowK8sApiOutbound",
    security_group_id=talos_lb_sg.id,
    cidr_ipv4=internal_ip_range,
    ip_protocol="tcp",
    from_port=6443,
    to_port=6443,
)
# allow traffic from LB to cluster
aws.vpc.SecurityGroupIngressRule(
    "allowFromLbToCluster",
    security_group_id=talos_sg.id,
    referenced_security_group_id=talos_lb_sg.id,
    ip_protocol="-1",
    from_port=0,
    to_port=65535,
)

# NLB code mainly came from: https://www.pulumi.com/ai/conversations/a58fa9dd-d682-4cc0-9705-8073bc895add
# plus a little CoPilot assist + intellisense
target_group = aws.lb.TargetGroup(
    "talosTargetGroup",
    port=6443,
    protocol="TCP",
    target_type="instance",
    vpc_id=vpc.vpc_id,
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        # path="/healthz",
        # port="6443",
        protocol="TCP",
    ),
)

# k8s_instance_type = "m5a.xlarge"
# https://help.pluralsight.com/hc/en-us/articles/24425443133076-AWS-cloud-sandbox
K8S_INSTANCE_TYPE = "t3a.medium"

cpInstances: list[aws.ec2.Instance] = []
for i in range(1):
    cpInstance = aws.ec2.Instance(
        f"talosCp{i}",
        associate_public_ip_address=True,
        instance_type=K8S_INSTANCE_TYPE,
        ami=talos_ami.id,
        subnet_id=vpc.public_subnet_ids[i],
        security_groups=[talos_sg.id],
        tags={"Name": f"talos-cp-{i}"},
        user_data=Path("./controlplane.yaml").read_text(),
    )
    aws.lb.TargetGroupAttachment(
        f"talosCp{i}Attachment",
        target_group_arn=target_group.arn,
        target_id=cpInstance.id,
        port=6443,
    )
    cpInstances.append(cpInstance)

nlb = aws.lb.LoadBalancer(
    "talosNlb",
    internal=False,
    load_balancer_type="network",
    subnets=vpc.public_subnet_ids,
    security_groups=[talos_lb_sg.id],
    # enable_deletion_protection=False,
    # enable_cross_zone_load_balancing=False,
    # enable_http2=False,
    tags={"Name": "talos-nlb"},
)

aws.lb.Listener(
    "talosListener",
    load_balancer_arn=nlb.arn,
    port=6443,
    protocol="TCP",
    default_actions=[
        aws.lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=target_group.arn,
        )
    ],
)

wkrInstances: list[aws.ec2.Instance] = []
for i in range(2):
    wkrInstance = aws.ec2.Instance(
        f"talosWkr{i}",
        associate_public_ip_address=True,
        instance_type=K8S_INSTANCE_TYPE,
        ami=talos_ami.id,
        subnet_id=vpc.private_subnet_ids[i],
        security_groups=[talos_sg.id],
        tags={"Name": f"talos-wkr-{i}"},
        user_data=Path("./worker.yaml").read_text(),
    )
    wkrInstances.append(wkrInstance)


# if need to modify yaml files created (From YT Vid): https://www.talos.dev/v1.7/reference/configuration/v1alpha1/config/


# Export a few properties to make them easy to use.
pulumi.export("vpcId", vpc.vpc_id)
pulumi.export("publicSubnetIds", vpc.public_subnet_ids)
pulumi.export("privateSubnetIds", vpc.private_subnet_ids)
pulumi.export("talosOwnerId", talos_ami.owner_id)
pulumi.export("talosArn", talos_ami.arn)
pulumi.export("nlbDnsName", nlb.dns_name)

ec2_instances = cpInstances + wkrInstances
for i, instance in enumerate(cpInstances):
    pulumi.export(f"cpInstance{i}Id", instance.id)
    pulumi.export(f"cpInstance{i}PublicIp", instance.public_ip)
    pulumi.export(f"instance{i}PrivateIp", instance.private_ip)
    # pulumi.export(f"cpInstance{i}PublicDns", instance.public_dns)
    # pulumi.export(f"instance{i}PrivateDns", instance.private_dns)
for i, instance in enumerate(wkrInstances):
    pulumi.export(f"wkrInstance{i}Id", instance.id)
    pulumi.export(f"wkrInstance{i}PublicIp", instance.public_ip)
    pulumi.export(f"wkrInstance{i}PrivateIp", instance.private_ip)
    # pulumi.export(f"wkrInstance{i}PublicDns", instance.public_dns)
    # pulumi.export(f"instance{i}PrivateDns", instance.private_dns)
