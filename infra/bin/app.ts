#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ClinicalCompassStack } from '../lib/clinicalcompass-stack';

const app = new cdk.App();

new ClinicalCompassStack(app, 'ClinicalCompassStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
  },
  description: 'ClinicalCompass — AI-powered clinical decision support system',
});
