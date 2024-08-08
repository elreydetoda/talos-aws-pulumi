"""An AWS Python Pulumi program"""

from pathlib import Path
from ipaddress import ip_network
from json import dumps as j_dumps
from requests import get as r_get
import pulumi
import pulumi_aws as aws
from pulumi_command.local import Command as l_cmd, Logging as l_log

# import pulumi_awsx as awsx

#################### Networking ####################
INTERNAL_IP_RANGE = ip_network("10.230.0.0/16")
INTERNAL_IP_RANGE_STR = str(INTERNAL_IP_RANGE)
# Define a VPC, mainly from: https://www.pulumi.com/ai/conversations/52d114ae-e54b-464d-9c5e-c31b9bdf5829
vpc = aws.ec2.Vpc(
    "custom-vpc",
    cidr_block=INTERNAL_IP_RANGE_STR,
    enable_dns_support=True,
    enable_dns_hostnames=True,
)

# Create an Internet Gateway for the VPC
internet_gateway = aws.ec2.InternetGateway(
    "internet-gateway",
    vpc_id=vpc.id,
)
aws.ec2.DefaultRouteTable(
    "default-route-table",
    default_route_table_id=vpc.main_route_table_id,
    routes=[
        aws.ec2.DefaultRouteTableRouteArgs(
            cidr_block="0.0.0.0/0",
            gateway_id=internet_gateway.id,
        ),
        aws.ec2.DefaultRouteTableRouteArgs(
            cidr_block=INTERNAL_IP_RANGE_STR,
            gateway_id="local",
        ),
    ],
)

# Create multiple public subnets
public_subnets: list[aws.ec2.Subnet] = []
LIMIT_ZONES = 3
SUBNET_NETMASK = 24
SUBNETS = list(INTERNAL_IP_RANGE.subnets(new_prefix=SUBNET_NETMASK))[:LIMIT_ZONES]
for current_zone_index, az in enumerate(aws.get_availability_zones().names):
    if current_zone_index >= LIMIT_ZONES:
        break
    curr_cidr_block = str(SUBNETS[current_zone_index])
    subnet = aws.ec2.Subnet(
        f"public-subnet-{az}",
        vpc_id=vpc.id,
        cidr_block=curr_cidr_block,
        availability_zone=az,
        map_public_ip_on_launch=True,
        # private_dns_hostname_type_on_launch="resource-name",
    )
    public_subnets.append(subnet)

public_subnet_ids = [subnet.id for subnet in public_subnets]

#################### AMI ####################
aws_region = aws.get_region()

talos_version = r_get(
    "https://api.github.com/repos/siderolabs/talos/releases/latest",
    timeout=5,
).json()["tag_name"]

ami_id_array: list[dict[str, str]] = r_get(
    # from: youtu.be/Erlfg6VhfJE
    f"https://github.com/siderolabs/talos/releases/download/{talos_version}/cloud-images.json",
    timeout=5,
).json()

# Filter down to the Talos AMI we need
for ami_id in ami_id_array:
    if ami_id["cloud"] == "aws":
        if ami_id["region"] == aws_region.name:
            if ami_id["arch"] == "amd64":
                talos_ami_id = ami_id["id"]
                break

talos_ami = aws.ec2.Ami.get("talos-ami", id=talos_ami_id)

#################### Security Groups ####################
talos_sg = aws.ec2.SecurityGroup(
    "talosSg",
    vpc_id=vpc.id,
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

current_ip = r_get("https://api.ipify.org/", timeout=5)
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
    vpc_id=vpc.id,
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
    cidr_ipv4=INTERNAL_IP_RANGE_STR,
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
    vpc_id=vpc.id,
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        # path="/healthz",
        # port="6443",
        protocol="TCP",
    ),
)

#################### Talos Command & NLB ####################
nlb = aws.lb.LoadBalancer(
    "talosNlb",
    internal=False,
    load_balancer_type="network",
    subnets=public_subnet_ids,
    security_groups=[talos_lb_sg.id],
    # enable_deletion_protection=False,
    # enable_cross_zone_load_balancing=False,
    # enable_http2=False,
    tags={"Name": "talos-nlb"},
)

# talos command derived from:
# https://github.com/siderolabs/talos/blob/bf1a87e9943b003eb3116be35807de4919b7cac9/website/content/v1.8/talos-guides/install/cloud-platforms/aws.md?plain=1#L266-L284
config_path = [
    {
        "op": "replace",
        "path": "/machine/time",
        "value": {
            "servers": ["169.254.169.123"],
        },
    }
]

# if need to modify yaml files created (From YT Vid): https://www.talos.dev/v1.7/reference/configuration/v1alpha1/config/
talos_configs = nlb.dns_name.apply(
    lambda dns_name: l_cmd(
        "talos_gen_configs",
        create=" ".join(
            [
                "talosctl",
                "gen",
                "config",
                "talos-k8s-aws-tutorial",
                f"https://{dns_name}:6443",
                "--with-examples=false",
                "--with-docs=false",
                "--install-disk=/dev/xvda",
                f"--config-patch='{j_dumps(config_path)}'",
                "--force",
            ]
        ),
        delete="rm -f talosconfig controlplane.yaml worker.yaml",
        logging=l_log.STDERR,
    )
)


# https://www.pulumi.com/ai/conversations/e2e0f548-e44f-4301-93c4-e23cf8c8ce39
# Read controlplane.yaml for control plane nodes
class ControlPlaneContentProvider(pulumi.dynamic.ResourceProvider):
    def create(self, props) -> pulumi.dynamic.ReadResult:
        file_content = Path("controlplane.yaml").read_text(encoding="utf-8")
        return pulumi.dynamic.ReadResult(id_="0", outs={"content": file_content})


class ControlPlaneContent(pulumi.dynamic.Resource):
    content: pulumi.Output[str]

    def __init__(self, name, opts=None):
        super().__init__(
            ControlPlaneContentProvider(),
            name,
            {
                "content": None,
            },
            opts,
        )


# Read worker.yml for worker nodes
class WorkerContentProvider(pulumi.dynamic.ResourceProvider):
    def create(self, props) -> pulumi.dynamic.ReadResult:
        file_content = Path("worker.yaml").read_text(encoding="utf-8")
        return pulumi.dynamic.ReadResult(id_="0", outs={"content": file_content})


class WorkerContent(pulumi.dynamic.Resource):
    content: pulumi.Output[str]

    def __init__(self, name, opts=None):
        super().__init__(
            WorkerContentProvider(),
            name,
            {
                "content": None,
            },
            opts,
        )


cp_file_content = ControlPlaneContent(
    "controlplane",
    opts=pulumi.ResourceOptions(depends_on=[talos_configs]),
)

w_file_content = WorkerContent(
    "worker",
    opts=pulumi.ResourceOptions(depends_on=[talos_configs]),
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
        subnet_id=public_subnet_ids[i],
        security_groups=[talos_sg.id],
        tags={"Name": f"talos-cp-{i}"},
        user_data=cp_file_content.content.apply(lambda content: content),
        opts=pulumi.ResourceOptions(depends_on=[talos_configs]),
    )
    aws.lb.TargetGroupAttachment(
        f"talosCp{i}Attachment",
        target_group_arn=target_group.arn,
        target_id=cpInstance.id,
        port=6443,
    )
    cpInstances.append(cpInstance)

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
        subnet_id=public_subnet_ids[i],
        security_groups=[talos_sg.id],
        tags={"Name": f"talos-wkr-{i}"},
        user_data=w_file_content.content.apply(lambda content: content),
        opts=pulumi.ResourceOptions(depends_on=[talos_configs]),
    )
    wkrInstances.append(wkrInstance)

# Export a few properties to make them easy to use.
pulumi.export("vpcId", vpc.id)
pulumi.export("publicSubnetIds", public_subnet_ids)
# pulumi.export("privateSubnetIds", vpc.private_subnet_ids)
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
