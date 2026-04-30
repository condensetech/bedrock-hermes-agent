"""AgentCore stack — IAM execution role, S3 bucket, security group.

Defines the IAM role that AgentCore containers assume, the S3 bucket for
per-user workspace persistence, and the security group for VPC networking.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    CfnOutput,
)
from constructs import Construct


class HermesAgentCoreStack(Stack):
    """IAM role, S3 user-files bucket, security group for AgentCore."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        kms_key_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- S3 bucket for user files ------------------------------------

        self.bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"{project}-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=None,  # Uses the default S3 key; override with kms_key_arn if desired.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="CleanupOldVersions",
                    noncurrent_version_expiration=Duration.days(90),
                ),
            ],
        )

        # ---- Security group ----------------------------------------------

        self.sg = ec2.SecurityGroup(
            self,
            "AgentCoreSG",
            vpc=vpc,
            description="AgentCore container security group",
            allow_all_outbound=True,
        )

        # ---- IAM execution role ------------------------------------------
        # This role is assumed by the AgentCore runtime containers.

        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{project}-execution-role",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                iam.AccountPrincipal(account),
            ),
        )

        # Bedrock model invocation.  Cross-region inference profiles (e.g.
        # `eu.anthropic.claude-opus-4-7`) can route requests to any region in
        # the profile's geo group, so the foundation-model permission must
        # cover all regions — not only the deploy region.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{account}:inference-profile/*",
                ],
            )
        )

        # Bedrock Guardrails.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockGuardrails",
                actions=["bedrock:ApplyGuardrail"],
                resources=[f"arn:aws:bedrock:{region}:{account}:guardrail/*"],
            )
        )

        # S3 — user files bucket.
        self.bucket.grant_read_write(self.execution_role)

        # Secrets Manager — read bot tokens and API keys.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/*",
                ],
            )
        )

        # STS — self-assume for scoped credentials.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SelfAssume",
                actions=["sts:AssumeRole"],
                resources=[self.execution_role.role_arn],
            )
        )

        # EventBridge Scheduler — agent uses this to manage its own
        # scheduled tasks (the ``schedule`` tool in app/hermes/main.py).
        # Scoped to the ``hermes-*`` name prefix so the agent can't read
        # or modify schedules outside its own namespace.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SchedulerManage",
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                ],
                resources=[
                    f"arn:aws:scheduler:{region}:{account}:schedule/default/hermes-*",
                ],
            )
        )
        # ListSchedules' NamePrefix filter is request-side, not an IAM
        # resource constraint — allow account-wide list. Content scoping
        # to the caller's namespace is enforced in the schedule tool.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SchedulerList",
                actions=["scheduler:ListSchedules"],
                resources=["*"],
            )
        )
        # Required so the agent can pass the scheduler-role ARN as
        # ``Target.RoleArn`` when creating a schedule (EventBridge then
        # assumes that role to invoke the cron lambda).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SchedulerPassRole",
                actions=["iam:PassRole"],
                resources=[
                    f"arn:aws:iam::{account}:role/{project}-scheduler-role",
                ],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "scheduler.amazonaws.com",
                    },
                },
            )
        )
        # STS GetCallerIdentity — used by the schedule tool to derive
        # the account ID at runtime when constructing the lambda /
        # scheduler-role ARNs.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="StsCallerIdentity",
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            )
        )

        # KMS — decrypt secrets and S3 objects.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsDecrypt",
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[kms_key_arn],
            )
        )

        # KMS — allow encryption/decryption of any S3-served object. The user-
        # files bucket uses an auto-created CDK KMS key (separate from the
        # project CMK above); rather than hard-wiring its ARN, scope the
        # permission to S3 via kms:ViaService.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsForS3",
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"s3.{region}.amazonaws.com",
                    },
                },
            )
        )

        # Observability — must mirror the perms agentcore-cdk's HarnessRole
        # wires up. Without DescribeLogStreams the container's log shipper
        # can't find the runtime's log stream and writes silently drop on
        # the floor (regressed v5→v6 when we replaced the CLI auto-role
        # with this CDK-managed one).
        runtime_lg = (
            f"arn:aws:logs:{region}:{account}:"
            f"log-group:/aws/bedrock-agentcore/runtimes/*"
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsGroup",
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[runtime_lg],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsDescribeGroups",
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsStream",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{runtime_lg}:log-stream:*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchMetricsPublish",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"},
                },
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="XRayTracingAccess",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )

        # ECR — pull container image.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRPull",
                actions=[
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=["*"],
            )
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        CfnOutput(self, "SecurityGroupId", value=self.sg.security_group_id)
