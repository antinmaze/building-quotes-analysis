targetScope = 'subscription'

@description('Stable environment identifier used for tags and deployment metadata.')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Azure region for the existing resource group and Cognitive Services accounts.')
@minLength(1)
param location string

@description('Existing resource group name. This template does not create or modify the resource group.')
param resourceGroupName string

@description('Subscription ID that the deployment is expected to target.')
param expectedSubscriptionId string

@description('Session identifier for AppOnboard traceability.')
param sessionId string

@description('Operator identity recorded in resource tags.')
param deployedBy string

@description('ISO 8601 UTC timestamp recorded in resource tags.')
param createdAt string

@description('Verified Microsoft Entra object ID for the operator receiving scoped data-plane roles.')
param deployerObjectId string

@description('Exact Document Intelligence account name from the approved plan.')
param documentIntelligenceName string

@description('Exact Azure OpenAI account name from the approved plan.')
param openAiName string

@description('Deployment name used by the local application.')
param openAiDeploymentName string = 'devis-extraction'

@description('Configurable Azure OpenAI model name. Keep the approved value unless compatibility is revalidated.')
param openAiModelName string = 'gpt-5-mini'

@description('Configurable Azure OpenAI model version. Keep the approved value unless compatibility is revalidated.')
param openAiModelVersion string = '2025-08-07'

@description('Global Standard deployment capacity in thousands of tokens per minute.')
param openAiCapacity int = 50

@description('Deployment SKU for the Azure OpenAI model.')
param openAiDeploymentSku string = 'GlobalStandard'

@description('Azure OpenAI API version consumed by the local SDK.')
param openAiApiVersion string = '2024-10-21'

@description('Application model revision marker for local cache/audit invalidation.')
param openAiModelRevision string = 'gpt-5-mini-2025-08-07'

resource existingResourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' existing = {
  name: resourceGroupName
  scope: subscription(expectedSubscriptionId)
}

module pilot './pilot.bicep' = {
  name: 'pilot'
  scope: existingResourceGroup
  params: {
    location: location
    environmentName: environmentName
    sessionId: sessionId
    deployedBy: deployedBy
    createdAt: createdAt
    deployerObjectId: deployerObjectId
    documentIntelligenceName: documentIntelligenceName
    openAiName: openAiName
    openAiDeploymentName: openAiDeploymentName
    openAiModelName: openAiModelName
    openAiModelVersion: openAiModelVersion
    openAiDeploymentSku: openAiDeploymentSku
    openAiCapacity: openAiCapacity
  }
}

output azureTenantId string = subscription().tenantId
output azureSubscriptionId string = subscription().subscriptionId
output subscriptionGuardPassed bool = subscription().subscriptionId == expectedSubscriptionId
output azureResourceGroup string = resourceGroupName
output azureLocation string = location
output existingResourceGroupLocation string = existingResourceGroup.location
output resourceGroupLocationMatches bool = toLower(existingResourceGroup.location) == toLower(location)
output azureDocumentIntelligenceEndpoint string = pilot.outputs.azureDocumentIntelligenceEndpoint
output azureOpenAiEndpoint string = pilot.outputs.azureOpenAiEndpoint
output azureOpenAiDeployment string = openAiDeploymentName
output azureOpenAiApiVersion string = openAiApiVersion
output azureOpenAiModelRevision string = openAiModelRevision
output azureOpenAiModelName string = openAiModelName
output azureOpenAiModelVersion string = openAiModelVersion
