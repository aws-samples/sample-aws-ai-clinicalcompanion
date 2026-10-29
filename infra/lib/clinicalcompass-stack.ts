import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwv2_integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as apigwv2_authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as opensearchserverless from 'aws-cdk-lib/aws-opensearchserverless';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as events from 'aws-cdk-lib/aws-events';
import * as events_targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudfront_origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cloudtrail from 'aws-cdk-lib/aws-cloudtrail';
import { Construct } from 'constructs';

interface KnowledgeBaseConfig {
  name: string;
  description: string;
  collectionName: string;
  indexName: string;
  bucketSuffix: string;
}

export class ClinicalCompassStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // ════════════════════════════════════════════════════════════
    // 1. DynamoDB — WebSocket connections table
    // ════════════════════════════════════════════════════════════

    const connectionsTable = new dynamodb.Table(this, 'ConnectionsTable', {
      tableName: 'clinicalcompass-websocket-connections',
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ════════════════════════════════════════════════════════════
    // 1b. KMS — Customer Managed Key for patient data encryption
    // ════════════════════════════════════════════════════════════

    const patientDataKey = new kms.Key(this, 'PatientDataKey', {
      enableKeyRotation: true,
      description: 'Customer managed key for encrypting patient data at rest in DynamoDB',
      alias: 'clinicalcompass/patient-data',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    cdk.Tags.of(patientDataKey).add('Purpose', 'Patient data encryption for ClinicalCompass');

    // ════════════════════════════════════════════════════════════
    // 1c. DynamoDB — Patient Index table (CMK encrypted)
    // ════════════════════════════════════════════════════════════

    const patientIndexTable = new dynamodb.Table(this, 'PatientIndexTable', {
      tableName: 'clinicalcompass-patient-index',
      partitionKey: { name: 'patient_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: patientDataKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ════════════════════════════════════════════════════════════
    // 1d. DynamoDB — Guideline Registry table
    // ════════════════════════════════════════════════════════════

    const guidelineRegistryTable = new dynamodb.Table(this, 'GuidelineRegistryTable', {
      tableName: 'clinicalcompass-guideline-registry',
      partitionKey: { name: 'source_name', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ════════════════════════════════════════════════════════════
    // 1e. S3 — EHR Data Bucket (with event notification support)
    // ════════════════════════════════════════════════════════════

    const ehrDataBucket = new s3.Bucket(this, 'EhrDataBucket', {
      bucketName: `clinicalcompass-ehr-data-${account}`,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      eventBridgeEnabled: true,
    });

    // ════════════════════════════════════════════════════════════
    // 1f. S3 — Guidelines Bucket (with event notification support)
    // ════════════════════════════════════════════════════════════

    const guidelinesBucket = new s3.Bucket(this, 'GuidelinesBucket', {
      bucketName: `clinicalcompass-guidelines-${account}`,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      eventBridgeEnabled: true,
    });

    // ════════════════════════════════════════════════════════════
    // 1g. HealthLake — FHIR Datastore
    // Created by post_cdk_setup.py (takes 5-10 min, too slow for CloudFormation)
    // The datastore ID is stored in SSM and read at runtime by the EHR Lambda.
    // ════════════════════════════════════════════════════════════

    // ════════════════════════════════════════════════════════════
    // 1h. Lambda — EHR Ingestion (S3 trigger on EHR bucket)
    // ════════════════════════════════════════════════════════════

    const ehrIngestionLambda = new lambda.Function(this, 'EhrIngestionLambda', {
      functionName: 'clinicalcompass-ehr-ingestion',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambda/ehr_ingestion'),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_YEAR,
      environment: {
        PATIENT_INDEX_TABLE: patientIndexTable.tableName,
        FHIR_DATASTORE_ID_SSM: '/clinicalcompass/fhir-datastore-id',
        FHIR_DATASTORE_ENDPOINT_SSM: '/clinicalcompass/fhir-datastore-endpoint',
        EHR_BUCKET_NAME: ehrDataBucket.bucketName,
      },
    });

    // S3 event notification: ObjectCreated → EHR Ingestion Lambda
    ehrDataBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(ehrIngestionLambda),
    );

    // IAM: S3 read on EHR bucket
    ehrDataBucket.grantRead(ehrIngestionLambda);

    // IAM: DynamoDB read/write on patient index table
    patientIndexTable.grantReadWriteData(ehrIngestionLambda);

    // IAM: HealthLake write (FHIR operations) — wildcard since datastore created by post_cdk_setup.py
    ehrIngestionLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'healthlake:CreateResource',
        'healthlake:UpdateResource',
        'healthlake:ReadResource',
        'healthlake:SearchWithPost',
        'healthlake:SearchWithGet',
      ],
      resources: [
        `arn:aws:healthlake:${region}:${account}:datastore/fhir/*`,
      ],
    }));

    // IAM: Bedrock KB sync (start/get ingestion jobs)
    ehrIngestionLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:StartIngestionJob',
        'bedrock:GetIngestionJob',
      ],
      resources: [`arn:aws:bedrock:${region}:${account}:knowledge-base/*`],
    }));

    // IAM: SSM read (for HealthLake datastore ID/endpoint at runtime)
    ehrIngestionLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${region}:${account}:parameter/clinicalcompass/*`],
    }));

    // SSM Parameter for EHR Ingestion Lambda ARN
    new ssm.StringParameter(this, 'EhrIngestionLambdaArnParam', {
      parameterName: '/clinicalcompass/ehr-ingestion-lambda-arn',
      stringValue: ehrIngestionLambda.functionArn,
      description: 'EHR Ingestion Lambda function ARN',
    });

    // ════════════════════════════════════════════════════════════
    // 2. Cognito — Authentication & Authorization
    // ════════════════════════════════════════════════════════════

    const userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: 'clinicalcompass-users',
      selfSignUpEnabled: false,
      signInAliases: { username: true, email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool,
      userPoolClientName: 'clinicalcompass-web',
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      generateSecret: false, // Public client for browser use
    });

    // ════════════════════════════════════════════════════════════
    // 3. ECR Repository
    // ════════════════════════════════════════════════════════════

    const ecrRepo = new ecr.Repository(this, 'AgentCoreEcr', {
      repositoryName: 'bedrock-agentcore-clinicalcompass',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    // ════════════════════════════════════════════════════════════
    // 3. SSM Parameters — placeholders updated by post-CDK scripts
    // ════════════════════════════════════════════════════════════

    new ssm.StringParameter(this, 'MemoryIdParam', {
      parameterName: '/clinicalcompass/stm-memory-id',
      stringValue: 'PLACEHOLDER',
      description: 'AgentCore STM Memory ID — updated by post_cdk_setup.py',
    });

    // AgentCore ARN — set by post_cdk_setup.py after runtime creation
    const agentArnParam = new ssm.StringParameter(this, 'AgentArnParam', {
      parameterName: '/clinicalcompass/agent-runtime-arn',
      stringValue: 'PLACEHOLDER',
      description: 'AgentCore Runtime ARN — updated by post_cdk_setup.py',
    });

    // SSM Parameters for clinical enhancement resources
    new ssm.StringParameter(this, 'PatientIndexTableParam', {
      parameterName: '/clinicalcompass/patient-index-table',
      stringValue: patientIndexTable.tableName,
      description: 'DynamoDB Patient Index table name',
    });

    new ssm.StringParameter(this, 'GuidelineRegistryTableParam', {
      parameterName: '/clinicalcompass/guideline-registry-table',
      stringValue: guidelineRegistryTable.tableName,
      description: 'DynamoDB Guideline Registry table name',
    });

    new ssm.StringParameter(this, 'EhrDataBucketParam', {
      parameterName: '/clinicalcompass/ehr-data-bucket',
      stringValue: ehrDataBucket.bucketName,
      description: 'S3 EHR data bucket name',
    });

    new ssm.StringParameter(this, 'GuidelinesBucketParam', {
      parameterName: '/clinicalcompass/guidelines-bucket',
      stringValue: guidelinesBucket.bucketName,
      description: 'S3 Guidelines bucket name',
    });

    new ssm.StringParameter(this, 'FhirDatastoreIdParam', {
      parameterName: '/clinicalcompass/fhir-datastore-id',
      stringValue: 'PLACEHOLDER',
      description: 'HealthLake FHIR datastore ID — updated by post_cdk_setup.py',
    });

    new ssm.StringParameter(this, 'FhirDatastoreEndpointParam', {
      parameterName: '/clinicalcompass/fhir-datastore-endpoint',
      stringValue: 'PLACEHOLDER',
      description: 'HealthLake FHIR datastore endpoint — updated by post_cdk_setup.py',
    });

    // ════════════════════════════════════════════════════════════
    // 4. IAM Role for AgentCore Runtime (created here, used by post-CDK script)
    // ════════════════════════════════════════════════════════════

    const agentCoreRole = new iam.Role(this, 'AgentCoreRole', {
      roleName: 'BedrockAgentCoreRuntime-clinicalcompass',
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    });

    // Allow invoking the US geographic cross-Region inference profile in the
    // source Region (this stack's region, e.g. us-east-1).
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeUsInferenceProfile',
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        `arn:aws:bedrock:${region}:${account}:inference-profile/*`,
      ],
    }));
    // A US cross-Region inference profile (us.* prefix) routes the underlying
    // model invocation to foundation models in ALL destination Regions
    // (us-east-1, us-east-2, us-west-2), not just this stack's Region. The
    // role must be granted InvokeModel* on the foundation model in every
    // destination Region, gated by the inference profile ARN so direct
    // per-Region model access is not granted (least privilege).
    // See: https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html
    const usCrisDestinationRegions = ['us-east-1', 'us-east-2', 'us-west-2'];
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeUsCrisDestinationModels',
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: usCrisDestinationRegions.map(
        (r) => `arn:aws:bedrock:${r}::foundation-model/*`,
      ),
      conditions: {
        StringLike: {
          'bedrock:InferenceProfileArn': `arn:aws:bedrock:${region}:${account}:inference-profile/*`,
        },
      },
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
      resources: [`arn:aws:bedrock:${region}:${account}:knowledge-base/*`],
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${region}:${account}:parameter/clinicalcompass/*`],
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      actions: ['aoss:APIAccessAll'],
      resources: [`arn:aws:aoss:${region}:${account}:collection/*`],
      conditions: { StringLike: { 'aoss:collection': 'cc-*' } },
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime', 'bedrock-agentcore:GetAgentRuntime', 'bedrock-agentcore:CreateEvent', 'bedrock-agentcore:GetEvent', 'bedrock-agentcore:ListEvents'],
      resources: [
        `arn:aws:bedrock-agentcore:${region}:${account}:runtime/clinicalcompass-*`,
        `arn:aws:bedrock-agentcore:${region}:${account}:memory/clinicalcompass_mem-*`,
      ],
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EcrAuthToken',
      actions: ['ecr:GetAuthorizationToken'],
      resources: ['*'], // GetAuthorizationToken does not support resource-level scoping
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EcrImagePull',
      actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'],
      resources: [ecrRepo.repositoryArn],
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*`,
        `arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*:*`,
      ],
    }));
    // Observability: export OpenTelemetry traces to X-Ray / CloudWatch Transaction Search.
    // Permissions per the AgentCore Runtime execution role documentation:
    // https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'XRayTraceExport',
      actions: [
        'xray:PutTraceSegments',
        'xray:PutTelemetryRecords',
        'xray:GetSamplingRules',
        'xray:GetSamplingTargets',
      ],
      resources: ['*'], // These X-Ray actions do not support resource-level scoping
    }));
    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AgentCoreMetrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'], // PutMetricData does not support resource-level scoping; limited by namespace below
      conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
    }));
    // S3 permissions are scoped to specific KB buckets below (after KB bucket creation)


    // ════════════════════════════════════════════════════════════
    // 4b. REST API — Patient and Guideline endpoints (HTTP API)
    // ════════════════════════════════════════════════════════════

    // Lambda: Patients API handler
    const patientsApiHandler = new lambda.Function(this, 'PatientsApiHandler', {
      functionName: 'clinicalcompass-patients-api',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda/patients_api'),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_YEAR,
      environment: {
        PATIENT_INDEX_TABLE_NAME: patientIndexTable.tableName,
      },
    });

    // Lambda: Guidelines API handler
    const guidelinesApiHandler = new lambda.Function(this, 'GuidelinesApiHandler', {
      functionName: 'clinicalcompass-guidelines-api',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda/guidelines_api'),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_YEAR,
      environment: {
        GUIDELINE_REGISTRY_TABLE_NAME: guidelineRegistryTable.tableName,
      },
    });

    // Grant DynamoDB read access
    patientIndexTable.grantReadData(patientsApiHandler);
    guidelineRegistryTable.grantReadData(guidelinesApiHandler);

    // HTTP API Gateway
    const httpApi = new apigwv2.HttpApi(this, 'ClinicalCompassHttpApi', {
      apiName: 'clinicalcompass-rest-api',
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.OPTIONS],
        allowHeaders: ['Content-Type', 'Authorization'],
      },
    });

    // ── JWT Authorizer for HTTP API (Cognito) ──
    const httpJwtAuthorizer = new apigwv2_authorizers.HttpJwtAuthorizer(
      'CognitoHttpAuthorizer',
      `https://cognito-idp.${region}.amazonaws.com/${userPool.userPoolId}`,
      { jwtAudience: [userPoolClient.userPoolClientId] },
    );

    // GET /patients — list all patients
    httpApi.addRoutes({
      path: '/patients',
      methods: [apigwv2.HttpMethod.GET],
      integration: new apigwv2_integrations.HttpLambdaIntegration(
        'PatientsListIntegration', patientsApiHandler
      ),
      authorizer: httpJwtAuthorizer,
    });

    // GET /patients/{patient_id} — get single patient
    httpApi.addRoutes({
      path: '/patients/{patient_id}',
      methods: [apigwv2.HttpMethod.GET],
      integration: new apigwv2_integrations.HttpLambdaIntegration(
        'PatientDetailIntegration', patientsApiHandler
      ),
      authorizer: httpJwtAuthorizer,
    });

    // GET /guidelines — list all guidelines
    httpApi.addRoutes({
      path: '/guidelines',
      methods: [apigwv2.HttpMethod.GET],
      integration: new apigwv2_integrations.HttpLambdaIntegration(
        'GuidelinesListIntegration', guidelinesApiHandler
      ),
      authorizer: httpJwtAuthorizer,
    });

    // SSM Parameter for API endpoint URL
    new ssm.StringParameter(this, 'HttpApiEndpointParam', {
      parameterName: '/clinicalcompass/http-api-endpoint',
      stringValue: httpApi.apiEndpoint,
      description: 'HTTP API Gateway endpoint URL',
    });

    // HTTP API stage throttling (burst=1000, rate=500)
    const httpApiDefaultStage = httpApi.defaultStage?.node.defaultChild as apigwv2.CfnStage;
    if (httpApiDefaultStage) {
      httpApiDefaultStage.defaultRouteSettings = {
        throttlingBurstLimit: 1000,
        throttlingRateLimit: 500,
      };
    }

    // ════════════════════════════════════════════════════════════
    // 4c. Lambda — Guideline Ingestion (S3 trigger)
    // ════════════════════════════════════════════════════════════

    const guidelineIngestionLambda = new lambda.Function(this, 'GuidelineIngestionLambda', {
      functionName: 'clinicalcompass-guideline-ingestion',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda/guideline_ingestion'),
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_YEAR,
      environment: {
        GUIDELINE_REGISTRY_TABLE: guidelineRegistryTable.tableName,
        KNOWLEDGE_BASE_ID: '', // Updated by post_cdk_setup.py via SSM
        DATA_SOURCE_ID: '',    // Updated by post_cdk_setup.py via SSM
        GUIDELINES_BUCKET: guidelinesBucket.bucketName,
      },
    });

    // Grant DynamoDB write access to guideline registry
    guidelineRegistryTable.grantWriteData(guidelineIngestionLambda);

    // Grant S3 read access on guidelines bucket
    guidelinesBucket.grantRead(guidelineIngestionLambda);

    // Grant Bedrock KB sync permissions
    guidelineIngestionLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:StartIngestionJob', 'bedrock:GetIngestionJob'],
      resources: [`arn:aws:bedrock:${region}:${account}:knowledge-base/*`],
    }));

    // S3 event notification: ObjectCreated → Guideline Ingestion Lambda
    guidelinesBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(guidelineIngestionLambda),
    );

    // SSM Parameter for Guideline Ingestion Lambda ARN
    new ssm.StringParameter(this, 'GuidelineIngestionLambdaArnParam', {
      parameterName: '/clinicalcompass/guideline-ingestion-lambda-arn',
      stringValue: guidelineIngestionLambda.functionArn,
      description: 'Guideline Ingestion Lambda function ARN',
    });

    // ════════════════════════════════════════════════════════════
    // 5. Lambda Functions — WebSocket handlers
    // ════════════════════════════════════════════════════════════

    // Least privilege (threat model T15): every WebSocket handler gets its own
    // role holding only the API calls that handler makes, so compromising one
    // function does not yield the permissions of the others. Only the query
    // handler can read patient data, decrypt the patient KMS key, or invoke
    // the agent.

    // Data-plane callback to clients; resolved lazily because wsApi is created below.
    const manageConnections = new iam.PolicyStatement({
      actions: ['execute-api:ManageConnections'],
      resources: [
        cdk.Lazy.string({ produce: () => `arn:aws:execute-api:${region}:${account}:${wsApi.apiId}/production/POST/@connections/*` }),
      ],
    });

    const connectionsTableAccess = (...actions: string[]) => new iam.PolicyStatement({
      actions: actions.map((a) => `dynamodb:${a}`),
      resources: [connectionsTable.tableArn],
    });

    const handlerConfigs: Record<string, {
      handler: string;
      timeout: number;
      memory: number;
      statements: iam.PolicyStatement[];
    }> = {
      // connect.py: put_item (connection record + Cognito sub)
      'clinicalcompass-ws-connect': {
        handler: 'connect.lambda_handler', timeout: 30, memory: 256,
        statements: [connectionsTableAccess('PutItem')],
      },
      // disconnect.py: delete_item
      'clinicalcompass-ws-disconnect': {
        handler: 'disconnect.lambda_handler', timeout: 30, memory: 256,
        statements: [connectionsTableAccess('DeleteItem')],
      },
      // ping.py: update_item (lastActiveAt) + post_to_connection
      'clinicalcompass-ws-ping': {
        handler: 'ping.lambda_handler', timeout: 30, memory: 256,
        statements: [connectionsTableAccess('UpdateItem'), manageConnections],
      },
      // default.py: post_to_connection only
      'clinicalcompass-ws-default': {
        handler: 'default.lambda_handler', timeout: 30, memory: 256,
        statements: [manageConnections],
      },
      // query.py: session get/update, rate-limit counter (update_item),
      // stale-connection cleanup (delete_item), patient context (get_item),
      // agent ARN lookup (SSM), streaming invoke, post_to_connection.
      'clinicalcompass-ws-query': {
        handler: 'query.lambda_handler', timeout: 900, memory: 1024,
        statements: [
          connectionsTableAccess('GetItem', 'UpdateItem', 'DeleteItem'),
          new iam.PolicyStatement({
            actions: ['dynamodb:GetItem'],
            resources: [patientIndexTable.tableArn],
          }),
          new iam.PolicyStatement({
            actions: ['ssm:GetParameter'],
            resources: [agentArnParam.parameterArn],
          }),
          new iam.PolicyStatement({
            actions: ['bedrock-agentcore:InvokeAgentRuntime'],
            resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/clinicalcompass-*`],
          }),
          manageConnections,
        ],
      },
    };

    const lambdaDir = '../backend/lambda/websocket';
    const lambdaFunctions: Record<string, lambda.Function> = {};

    for (const [funcName, cfg] of Object.entries(handlerConfigs)) {
      const role = new iam.Role(this, `${funcName.replace(/-/g, '')}Role`, {
        roleName: `${funcName}-role`,
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });
      cfg.statements.forEach((s) => role.addToPolicy(s));

      const env: Record<string, string> = {
        CONNECTIONS_TABLE_NAME: connectionsTable.tableName,
      };

      // Query handler reads AGENT_ARN from SSM (set by post_cdk_setup.py)
      if (funcName === 'clinicalcompass-ws-query') {
        env.AGENT_ARN_SSM_PARAM = agentArnParam.parameterName;
        env.AWS_BEDROCK_REGION = region;
        // Patient index is encrypted with the CMK; only the query role may decrypt.
        patientDataKey.grant(role, 'kms:Decrypt');
      }

      lambdaFunctions[funcName] = new lambda.Function(this, funcName.replace(/-/g, ''), {
        functionName: funcName,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: cfg.handler,
        code: lambda.Code.fromAsset(lambdaDir),
        role,
        timeout: cdk.Duration.seconds(cfg.timeout),
        memorySize: cfg.memory,
        logRetention: logs.RetentionDays.ONE_YEAR,
        environment: env,
      });
    }

    // AgentCore reads patient context from the CMK-encrypted patient index.
    patientDataKey.grant(agentCoreRole, 'kms:Decrypt');

    // ════════════════════════════════════════════════════════════
    // 6. WebSocket API Gateway
    // ════════════════════════════════════════════════════════════

    // ── WebSocket $connect Lambda Authorizer ──
    // Validates JWT from query string since WebSocket API doesn't support
    // native JWT authorizers on the $connect route.
    const wsAuthorizerFn = new lambda.Function(this, 'WsAuthorizerFn', {
      functionName: 'clinicalcompass-ws-authorizer',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambda/websocket_authorizer'),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_YEAR,
      environment: {
        USER_POOL_ID: userPool.userPoolId,
        USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
      },
    });

    const wsAuthorizer = new apigwv2_authorizers.WebSocketLambdaAuthorizer(
      'WsConnectAuthorizer',
      wsAuthorizerFn,
      { identitySource: ['route.request.querystring.Authorization'] },
    );

    const wsApi = new apigwv2.WebSocketApi(this, 'WebSocketApi', {
      apiName: 'clinicalcompass-websocket',
      routeSelectionExpression: '$request.body.action',
      connectRouteOptions: {
        integration: new apigwv2_integrations.WebSocketLambdaIntegration(
          'ConnectIntegration', lambdaFunctions['clinicalcompass-ws-connect']
        ),
        authorizer: wsAuthorizer,
      },
      disconnectRouteOptions: {
        integration: new apigwv2_integrations.WebSocketLambdaIntegration(
          'DisconnectIntegration', lambdaFunctions['clinicalcompass-ws-disconnect']
        ),
      },
      defaultRouteOptions: {
        integration: new apigwv2_integrations.WebSocketLambdaIntegration(
          'DefaultIntegration', lambdaFunctions['clinicalcompass-ws-default']
        ),
      },
    });

    wsApi.addRoute('sendquery', {
      integration: new apigwv2_integrations.WebSocketLambdaIntegration(
        'QueryIntegration', lambdaFunctions['clinicalcompass-ws-query']
      ),
    });

    wsApi.addRoute('ping', {
      integration: new apigwv2_integrations.WebSocketLambdaIntegration(
        'PingIntegration', lambdaFunctions['clinicalcompass-ws-ping']
      ),
    });

    const wsStage = new apigwv2.WebSocketStage(this, 'ProductionStage', {
      webSocketApi: wsApi,
      stageName: 'production',
      autoDeploy: true,
      throttle: {
        burstLimit: 100,
        rateLimit: 50,
      },
    });

    // ════════════════════════════════════════════════════════════
    // 6b. AgentCore Pre-Warming — CloudWatch Events keep-alive
    // ════════════════════════════════════════════════════════════

    // Lambda function to ping AgentCore container every 5 minutes (keep-alive)
    const agentCoreKeepAliveLambda = new lambda.Function(this, 'AgentCoreKeepAliveLambda', {
      functionName: 'clinicalcompass-agentcore-keepalive',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      logRetention: logs.RetentionDays.ONE_YEAR,
      code: lambda.Code.fromInline(`
import boto3
import json
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    """Keep-alive ping to AgentCore container to reduce cold start frequency."""
    ssm = boto3.client('ssm')
    try:
        # Read AgentCore runtime ARN from SSM
        response = ssm.get_parameter(Name='/clinicalcompass/agent-runtime-arn')
        agent_arn = response['Parameter']['Value']
        if agent_arn == 'PLACEHOLDER':
            logger.info("AgentCore ARN not yet configured, skipping keep-alive ping")
            return {"statusCode": 200, "body": "Skipped - ARN not configured"}

        # Invoke AgentCore with a lightweight health-check payload
        client = boto3.client('bedrock-agentcore')
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            payload=json.dumps({"prompt": "health-check", "keepAlive": True}).encode('utf-8'),
        )
        logger.info(f"AgentCore keep-alive ping successful")
        return {"statusCode": 200, "body": "Keep-alive ping sent"}
    except Exception as e:
        logger.warning(f"AgentCore keep-alive ping failed (non-critical): {e}")
        return {"statusCode": 200, "body": f"Ping failed: {e}"}
`),
      environment: {
        AWS_BEDROCK_REGION: region,
      },
    });

    // Grant SSM read and AgentCore invoke permissions
    agentCoreKeepAliveLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${region}:${account}:parameter/clinicalcompass/*`],
    }));
    agentCoreKeepAliveLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:*`],
    }));

    // CloudWatch Events rule: invoke every 5 minutes
    const keepAliveRule = new events.Rule(this, 'AgentCoreKeepAliveRule', {
      ruleName: 'clinicalcompass-agentcore-keepalive',
      description: 'Pings AgentCore container every 5 minutes to reduce cold start frequency',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    keepAliveRule.addTarget(new events_targets.LambdaFunction(agentCoreKeepAliveLambda));


    // ════════════════════════════════════════════════════════════
    // 7. Knowledge Base Infrastructure
    // ════════════════════════════════════════════════════════════

    const kbConfigs: KnowledgeBaseConfig[] = [
      { name: 'ClinicalCompass-EHR-Mock', description: 'Mock EHR patient data', collectionName: 'cc-ehr-mock', indexName: 'cc-ehr-mock', bucketSuffix: 'ehr-mock' },
      { name: 'ClinicalCompass-Global-Guidelines', description: 'International clinical guidelines (ACR, ESUR, NICE, WHO)', collectionName: 'cc-global-guidelines', indexName: 'cc-global-guidelines', bucketSuffix: 'global-guidelines' },
      { name: 'ClinicalCompass-Local-Regulatory', description: 'National and local regulatory protocols (HAS France)', collectionName: 'cc-local-regulatory', indexName: 'cc-local-regulatory', bucketSuffix: 'local-regulatory' },
    ];

    const kbBucketArns: string[] = [];

    for (const kbCfg of kbConfigs) {
      const safeName = kbCfg.collectionName.replace(/-/g, '');

      // S3 bucket
      const bucket = new s3.Bucket(this, `KBBucket${safeName}`, {
        bucketName: `clinicalcompass-${kbCfg.bucketSuffix}-${account}`,
        versioned: true,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      // Collect KB bucket ARNs for scoped AgentCore S3 permissions
      kbBucketArns.push(bucket.bucketArn, `${bucket.bucketArn}/*`);

      // OpenSearch policies — collection MUST depend on these
      const encPolicy = new opensearchserverless.CfnSecurityPolicy(this, `EncPolicy${safeName}`, {
        name: `${safeName}-enc`.substring(0, 32),
        type: 'encryption',
        policy: JSON.stringify({
          Rules: [{ ResourceType: 'collection', Resource: [`collection/${kbCfg.collectionName}`] }],
          AWSOwnedKey: true,
        }),
      });

      const netPolicy = new opensearchserverless.CfnSecurityPolicy(this, `NetPolicy${safeName}`, {
        name: `${safeName}-net`.substring(0, 32),
        type: 'network',
        policy: JSON.stringify([{
          Rules: [
            { ResourceType: 'collection', Resource: [`collection/${kbCfg.collectionName}`] },
            { ResourceType: 'dashboard', Resource: [`collection/${kbCfg.collectionName}`] },
          ],
          AllowFromPublic: true,
        }]),
      });

      const dataPolicy = new opensearchserverless.CfnAccessPolicy(this, `DataPolicy${safeName}`, {
        name: `${safeName}-data`.substring(0, 32),
        type: 'data',
        policy: JSON.stringify([{
          Rules: [
            {
              ResourceType: 'collection',
              Resource: [`collection/${kbCfg.collectionName}`],
              Permission: ['aoss:CreateCollectionItems', 'aoss:DeleteCollectionItems', 'aoss:UpdateCollectionItems', 'aoss:DescribeCollectionItems'],
            },
            {
              ResourceType: 'index',
              Resource: [`index/${kbCfg.collectionName}/*`],
              Permission: ['aoss:CreateIndex', 'aoss:DeleteIndex', 'aoss:UpdateIndex', 'aoss:DescribeIndex', 'aoss:ReadDocument', 'aoss:WriteDocument'],
            },
          ],
          Principal: [
            `arn:aws:iam::${account}:root`,
            agentCoreRole.roleArn,
            `arn:aws:iam::${account}:role/BedrockKB-${kbCfg.name}-Role`,
          ],
        }]),
      });

      // Collection — explicit dependency on all 3 policies
      const collection = new opensearchserverless.CfnCollection(this, `Collection${safeName}`, {
        name: kbCfg.collectionName,
        type: 'VECTORSEARCH',
        description: kbCfg.description,
      });
      collection.addDependency(encPolicy);
      collection.addDependency(netPolicy);
      collection.addDependency(dataPolicy);

      // KB IAM role
      const kbRole = new iam.Role(this, `KBRole${safeName}`, {
        roleName: `BedrockKB-${kbCfg.name}-Role`,
        assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
          conditions: {
            StringEquals: { 'aws:SourceAccount': account },
            ArnLike: { 'aws:SourceArn': `arn:aws:bedrock:${region}:${account}:knowledge-base/*` },
          },
        }),
      });
      bucket.grantRead(kbRole);
      kbRole.addToPolicy(new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel'],
        resources: [`arn:aws:bedrock:${region}::foundation-model/amazon.titan-embed-text-v2:0`],
      }));
      kbRole.addToPolicy(new iam.PolicyStatement({
        actions: ['aoss:APIAccessAll'],
        resources: [`arn:aws:aoss:${region}:${account}:collection/*`],
      }));

      // NOTE: Bedrock Knowledge Base + DataSource + OpenSearch index creation
      // are handled by post_cdk_setup.py because CloudFormation cannot create
      // OpenSearch indices (required before KB creation).
    }

    // ════════════════════════════════════════════════════════════
    // 7b. AgentCore S3 permissions — scoped to Knowledge Base buckets
    // ════════════════════════════════════════════════════════════

    agentCoreRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3KnowledgeBaseBucketAccess',
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: kbBucketArns,
    }));

    // ════════════════════════════════════════════════════════════
    // 8. Frontend Hosting — S3 + CloudFront
    // ════════════════════════════════════════════════════════════

    // ── 8a. WAF WebACL — IP-based rate limiting for CloudFront ──

    const wafWebAcl = new wafv2.CfnWebACL(this, 'CloudFrontWafWebAcl', {
      name: 'clinicalcompass-cloudfront-waf',
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'clinicalcompass-waf',
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'IPRateLimit',
          priority: 1,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 1000,
              aggregateKeyType: 'IP',
              evaluationWindowSec: 300,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'clinicalcompass-ip-rate-limit',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // WAF CloudWatch Log Group (must start with "aws-waf-logs-")
    const wafLogGroup = new logs.LogGroup(this, 'WafLogGroup', {
      logGroupName: 'aws-waf-logs-clinicalcompass',
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // WAF Logging Configuration
    new wafv2.CfnLoggingConfiguration(this, 'WafLoggingConfig', {
      resourceArn: wafWebAcl.attrArn,
      logDestinationConfigs: [wafLogGroup.logGroupArn],
    });

    // ── 8b. S3 + CloudFront Distribution ──

    const frontendBucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName: `clinicalcompass-frontend-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: false, // Cleanup handled by cleanup_all.py
    });

    // CloudFront Origin Access Control (OAC) for S3
    const oac = new cloudfront.S3OriginAccessControl(this, 'FrontendOAC', {
      originAccessControlName: 'clinicalcompass-frontend-oac',
      signing: cloudfront.Signing.SIGV4_ALWAYS,
    });

    // Security Response Headers Policy (HSTS, X-Content-Type-Options, X-Frame-Options)
    const securityHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeadersPolicy', {
      responseHeadersPolicyName: 'clinicalcompass-security-headers',
      comment: 'Security headers for ClinicalCompass (HSTS, X-Content-Type-Options, X-Frame-Options)',
      securityHeadersBehavior: {
        strictTransportSecurity: {
          override: true,
          accessControlMaxAge: cdk.Duration.seconds(31536000),
          includeSubdomains: true,
        },
        contentTypeOptions: {
          override: true,
        },
        frameOptions: {
          override: true,
          frameOption: cloudfront.HeadersFrameOption.DENY,
        },
      },
    });

    const distribution = new cloudfront.Distribution(this, 'FrontendDistribution', {
      comment: 'ClinicalCompass Frontend',
      defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      webAclId: wafWebAcl.attrArn,
      defaultBehavior: {
        origin: cloudfront_origins.S3BucketOrigin.withOriginAccessControl(frontendBucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED, // Dev: no cache; switch to OPTIMIZED for prod
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        responseHeadersPolicy: securityHeadersPolicy,
      },
      errorResponses: [
        {
          httpStatus: 403,
          responsePagePath: '/index.html',
          responseHttpStatus: 200,
          ttl: cdk.Duration.seconds(0),
        },
        {
          httpStatus: 404,
          responsePagePath: '/index.html',
          responseHttpStatus: 200,
          ttl: cdk.Duration.seconds(0),
        },
      ],
    });

    // SSM Parameter for CloudFront URL (used by deploy_frontend.py)
    new ssm.StringParameter(this, 'CloudFrontUrlParam', {
      parameterName: '/clinicalcompass/cloudfront-url',
      stringValue: `https://${distribution.distributionDomainName}`,
      description: 'CloudFront distribution URL for frontend',
    });

    new ssm.StringParameter(this, 'CloudFrontDistributionIdParam', {
      parameterName: '/clinicalcompass/cloudfront-distribution-id',
      stringValue: distribution.distributionId,
      description: 'CloudFront distribution ID for cache invalidation',
    });

    new ssm.StringParameter(this, 'FrontendBucketParam', {
      parameterName: '/clinicalcompass/frontend-bucket',
      stringValue: frontendBucket.bucketName,
      description: 'S3 bucket for frontend static files',
    });

    // ════════════════════════════════════════════════════════════
    // 8c. Audit Logging & Security Monitoring
    // ════════════════════════════════════════════════════════════

    // ── SNS Topic for Security Alerts ──
    const securityAlertsTopic = new sns.Topic(this, 'SecurityAlertsTopic', {
      topicName: 'clinicalcompass-security-alerts',
      displayName: 'ClinicalCompass Security Alerts',
    });

    const securityAlertEmail = this.node.tryGetContext('securityAlertEmail');
    if (securityAlertEmail) {
      securityAlertsTopic.addSubscription(
        new sns_subscriptions.EmailSubscription(securityAlertEmail),
      );
    }

    // ── CloudTrail with data events for Patient_Index_Table and Guidelines_Bucket ──
    const auditTrailBucket = new s3.Bucket(this, 'AuditTrailBucket', {
      bucketName: `clinicalcompass-audit-trail-${account}`,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        { expiration: cdk.Duration.days(365) },
      ],
    });

    const auditTrail = new cloudtrail.Trail(this, 'AuditTrail', {
      trailName: 'clinicalcompass-data-audit',
      bucket: auditTrailBucket,
      isMultiRegionTrail: false,
      includeGlobalServiceEvents: false,
      enableFileValidation: true,
    });

    // Use Advanced Event Selectors for S3 data events on Guidelines Bucket
    const cfnTrail = auditTrail.node.defaultChild as cloudtrail.CfnTrail;
    // Remove default EventSelectors in favor of AdvancedEventSelectors
    cfnTrail.addPropertyOverride('EventSelectors', undefined);
    cfnTrail.addPropertyOverride('AdvancedEventSelectors', [
      {
        Name: 'GuidelinesBucketDataEvents',
        FieldSelectors: [
          { Field: 'eventCategory', Equals: ['Data'] },
          { Field: 'resources.type', Equals: ['AWS::S3::Object'] },
          { Field: 'resources.ARN', StartsWith: [`${guidelinesBucket.bucketArn}/`] },
        ],
      },
    ]);

    // ── Metric Filter + Alarm: 401 responses (failed authentication) ──
    // Filter on the WebSocket authorizer Lambda log group for 401/Unauthorized responses
    const wsAuthorizerLogGroup = wsAuthorizerFn.logGroup;

    const unauthorizedMetricFilter = new logs.MetricFilter(this, 'UnauthorizedAccessFilter', {
      logGroup: wsAuthorizerLogGroup,
      metricNamespace: 'ClinicalCompass/Security',
      metricName: 'UnauthorizedAccessCount',
      filterPattern: logs.FilterPattern.anyTerm('Unauthorized', '401', 'Deny', 'denied'),
      metricValue: '1',
    });

    const unauthorizedAlarm = new cloudwatch.Alarm(this, 'UnauthorizedAccessAlarm', {
      alarmName: 'clinicalcompass-unauthorized-access',
      alarmDescription: 'Triggers when 5+ failed authentication attempts occur within 5 minutes',
      metric: unauthorizedMetricFilter.metric({
        statistic: 'Sum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    unauthorizedAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(securityAlertsTopic));

    // ── Metric Filter + Alarm: High query volume (100+/min sustained 5 min) ──
    const queryLambdaLogGroup = lambdaFunctions['clinicalcompass-ws-query'].logGroup;

    const highQueryVolumeFilter = new logs.MetricFilter(this, 'HighQueryVolumeFilter', {
      logGroup: queryLambdaLogGroup,
      metricNamespace: 'ClinicalCompass/Security',
      metricName: 'QueryRequestCount',
      filterPattern: logs.FilterPattern.exists('$.action'),
      metricValue: '1',
    });

    const highQueryVolumeAlarm = new cloudwatch.Alarm(this, 'HighQueryVolumeAlarm', {
      alarmName: 'clinicalcompass-high-query-volume',
      alarmDescription: 'Triggers when query volume exceeds 100/min sustained for 5 minutes',
      metric: highQueryVolumeFilter.metric({
        statistic: 'Sum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 100,
      evaluationPeriods: 5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    highQueryVolumeAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(securityAlertsTopic));

    // ════════════════════════════════════════════════════════════
    // 8d. S3 Guideline Tamper Detection Infrastructure
    // ════════════════════════════════════════════════════════════

    // Tamper Detector Lambda — evaluates S3 events and alerts on unauthorized access
    const tamperDetectorLambda = new lambda.Function(this, 'TamperDetectorLambda', {
      functionName: 'clinicalcompass-tamper-detector',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda/tamper_detector'),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        SECURITY_ALERTS_TOPIC_ARN: securityAlertsTopic.topicArn,
        ALLOWED_ROLES: JSON.stringify([
          guidelineIngestionLambda.role!.roleArn,
        ]),
        GUIDELINES_BUCKET: guidelinesBucket.bucketName,
      },
      logRetention: logs.RetentionDays.ONE_YEAR,
    });

    // Grant SNS publish permission to tamper detector
    securityAlertsTopic.grantPublish(tamperDetectorLambda);

    // EventBridge rule: match S3 Object Created and Object Deleted on guidelinesBucket
    const tamperDetectionRule = new events.Rule(this, 'GuidelineTamperDetectionRule', {
      ruleName: 'clinicalcompass-guideline-tamper-detection',
      description: 'Triggers tamper detector on S3 object modifications to guidelines bucket',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created', 'Object Deleted'],
        detail: {
          bucket: { name: [guidelinesBucket.bucketName] },
        },
      },
    });
    tamperDetectionRule.addTarget(new events_targets.LambdaFunction(tamperDetectorLambda));

    // ════════════════════════════════════════════════════════════
    // 8e. Bedrock Guardrails — Content filtering and PII protection
    // ════════════════════════════════════════════════════════════

    const bedrockGuardrail = new cdk.CfnResource(this, 'ClinicalGuardrail', {
      type: 'AWS::Bedrock::Guardrail',
      properties: {
        Name: 'clinicalcompass-content-guardrail',
        Description: 'Content filters for harmful content blocking and PII extraction prevention',
        BlockedInputMessaging: 'Your query has been blocked for safety reasons. Please rephrase your question about clinical guidelines.',
        BlockedOutputsMessaging: "I'm unable to provide a response to that query. Please rephrase your question about clinical guidelines.",
        ContentPolicyConfig: {
          FiltersConfig: [
            { Type: 'HATE', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
            { Type: 'INSULTS', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
            { Type: 'SEXUAL', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
            { Type: 'VIOLENCE', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
            { Type: 'MISCONDUCT', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
          ],
        },
        SensitiveInformationPolicyConfig: {
          PiiEntitiesConfig: [
            { Type: 'NAME', Action: 'BLOCK' },
            { Type: 'PHONE', Action: 'BLOCK' },
            { Type: 'ADDRESS', Action: 'BLOCK' },
            { Type: 'US_SOCIAL_SECURITY_NUMBER', Action: 'BLOCK' },
            { Type: 'EMAIL', Action: 'BLOCK' },
          ],
        },
      },
    });

    // Pass Guardrail ID and version to the query Lambda as environment variables
    const queryLambdaFn = lambdaFunctions['clinicalcompass-ws-query'];
    queryLambdaFn.addEnvironment('BEDROCK_GUARDRAIL_ID', bedrockGuardrail.getAtt('GuardrailId').toString());
    queryLambdaFn.addEnvironment('BEDROCK_GUARDRAIL_VERSION', 'DRAFT');

    // Also pass the Patient Index Table name to query Lambda for patient context
    queryLambdaFn.addEnvironment('PATIENT_INDEX_TABLE_NAME', patientIndexTable.tableName);

    // ════════════════════════════════════════════════════════════
    // 9. Outputs
    // ════════════════════════════════════════════════════════════

    new cdk.CfnOutput(this, 'WebSocketUrl', { value: wsStage.url, description: 'WebSocket API URL' });
    new cdk.CfnOutput(this, 'WebSocketApiId', { value: wsApi.apiId, description: 'WebSocket API ID' });
    new cdk.CfnOutput(this, 'EcrRepositoryUri', { value: ecrRepo.repositoryUri, description: 'ECR Repository URI' });
    new cdk.CfnOutput(this, 'AgentCoreRoleArn', { value: agentCoreRole.roleArn, description: 'AgentCore IAM Role ARN (for post-CDK runtime creation)' });
    new cdk.CfnOutput(this, 'ConnectionsTableName', { value: connectionsTable.tableName, description: 'DynamoDB connections table' });
    new cdk.CfnOutput(this, 'CognitoUserPoolId', { value: userPool.userPoolId, description: 'Cognito User Pool ID' });
    new cdk.CfnOutput(this, 'CognitoClientId', { value: userPoolClient.userPoolClientId, description: 'Cognito App Client ID' });
    new cdk.CfnOutput(this, 'PatientIndexTableName', { value: patientIndexTable.tableName, description: 'DynamoDB Patient Index table' });
    new cdk.CfnOutput(this, 'GuidelineRegistryTableName', { value: guidelineRegistryTable.tableName, description: 'DynamoDB Guideline Registry table' });
    new cdk.CfnOutput(this, 'EhrDataBucketName', { value: ehrDataBucket.bucketName, description: 'S3 EHR data bucket' });
    new cdk.CfnOutput(this, 'GuidelinesBucketName', { value: guidelinesBucket.bucketName, description: 'S3 Guidelines bucket' });
    new cdk.CfnOutput(this, 'FhirDatastoreId', { value: 'Created by post_cdk_setup.py', description: 'HealthLake FHIR datastore ID' });
    new cdk.CfnOutput(this, 'FhirDatastoreEndpoint', { value: 'Created by post_cdk_setup.py', description: 'HealthLake FHIR datastore endpoint' });
    new cdk.CfnOutput(this, 'HttpApiEndpoint', { value: httpApi.apiEndpoint, description: 'HTTP API Gateway endpoint URL' });
    new cdk.CfnOutput(this, 'FrontendBucketName', { value: frontendBucket.bucketName, description: 'S3 bucket for frontend static files' });
    new cdk.CfnOutput(this, 'CloudFrontUrl', { value: `https://${distribution.distributionDomainName}`, description: 'CloudFront distribution URL' });
    new cdk.CfnOutput(this, 'CloudFrontDistributionId', { value: distribution.distributionId, description: 'CloudFront distribution ID' });
    new cdk.CfnOutput(this, 'SecurityAlertsTopicArn', { value: securityAlertsTopic.topicArn, description: 'SNS topic for security alerts' });
  }
}
