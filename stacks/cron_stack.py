"""Cron stack — EventBridge Scheduler + Cron executor Lambda.

The cron lambda receives schedule fires, deduplicates them via DDB, and
async-invokes the router lambda's ``_dispatch_request`` entry point. The
router does the AgentCore invocation and channel delivery.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct


class HermesCronStack(Stack):
    """EventBridge Scheduler + Lambda executor for scheduled tasks."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        identity_table_name: str,
        router_function_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- Cron executor Lambda ----------------------------------------
        # Receives EventBridge schedule fires, deduplicates same-jobId
        # firings via DDB claim, then async-invokes the router lambda's
        # `_dispatch_request` path so the agent runs share the user's
        # main-channel session (same lock + queue as Discord).

        self.cron_fn = lambda_.Function(
            self,
            "CronFn",
            function_name=f"{project}-cron",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/cron"),
            # 1 minute: cron lambda only does claim + async-invoke now,
            # the agent run happens inside the router lambda.
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "IDENTITY_TABLE": identity_table_name,
                "ROUTER_FUNCTION_NAME": router_function_name,
                # Stamped at deploy time by scripts/deploy.sh — surfaces in
                # Sentry events as the `release` field.
                "RELEASE_SHA": self.node.try_get_context("release_sha") or "",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Allow Lambda to async-invoke the router lambda — the only
        # dispatch path. Cron runs share the user's normal lock + queue
        # + AgentCore session as live channel messages.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeRouter",
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{region}:{account}:function:{router_function_name}",
                ],
            )
        )

        # Allow Lambda to read/write the identity table — needed for the
        # CRONFIRE# claim records that deduplicate same-jobId firings.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="CronClaimTable",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[
                    f"arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}",
                ],
            )
        )

        # Sentry DSN lives in Secrets Manager; no other secrets are read
        # from this lambda since delivery is handled by the router.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/sentry-dsn-cron-*",
                ],
            )
        )

        # Secrets are encrypted with the project CMK; Secrets Manager calls
        # kms:Decrypt on the caller's behalf. Scope the grant to Secrets
        # Manager via kms:ViaService.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"secretsmanager.{region}.amazonaws.com",
                    },
                },
            )
        )

        # ---- EventBridge Scheduler role ----------------------------------
        # Schedules are created dynamically via the agent or console.
        # This role allows EventBridge to invoke the Lambda.

        self.scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{project}-scheduler-role",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.cron_fn.grant_invoke(self.scheduler_role)

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "CronFunctionArn", value=self.cron_fn.function_arn)
        CfnOutput(self, "SchedulerRoleArn", value=self.scheduler_role.role_arn)
