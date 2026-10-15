targetScope = 'resourceGroup'

param location string = resourceGroup().location
param environmentName string = 'devis-mngt-dev-ed16'
param sessionId string
param deployedBy string
param createdAt string
param deployerObjectId string
param documentIntelligenceName string = 'di-devis-mngt-dev-ed16'
param openAiName string = 'aoai-devis-mngt-dev-ed16'
param openAiDeploymentName string = 'devis-extraction'
param openAiModelName string = 'gpt-5-mini'
param openAiModelVersion string = '2025-08-07'
@minValue(1)
@maxValue(50)
param openAiCapacity int = 50
@allowed([
  'GlobalStandard'
])
param openAiDeploymentSku string = 'GlobalStandard'

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

module documentIntelligence './modules/document-intelligence.bicep' = {
  name: 'document-intelligence'
  params: {
    location: location
    accountName: documentIntelligenceName
    tags: tags
  }
}

module openAi './modules/openai.bicep' = {
  name: 'openai'
  params: {
    location: location
    accountName: openAiName
    deploymentName: openAiDeploymentName
    modelName: openAiModelName
    modelVersion: openAiModelVersion
    deploymentSku: openAiDeploymentSku
    capacity: openAiCapacity
    tags: tags
  }
}

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  dependsOn: [
    documentIntelligence
    openAi
  ]
  params: {
    deployerObjectId: deployerObjectId
    documentIntelligenceAccountName: documentIntelligenceName
    openAiAccountName: openAiName
  }
}

output azureDocumentIntelligenceEndpoint string = documentIntelligence.outputs.endpoint
output azureOpenAiEndpoint string = openAi.outputs.endpoint
output azureOpenAiDeployment string = openAiDeploymentName
output azureOpenAiApiVersion string = '2024-10-21'
output azureOpenAiModelRevision string = '${openAiModelName}-${openAiModelVersion}'
