@description('Azure region for the Azure OpenAI account.')
param location string

@description('Exact globally unique account name.')
param accountName string

@description('Deployment name used by the local application.')
param deploymentName string

@description('Configurable model name.')
param modelName string

@description('Configurable model version.')
param modelVersion string

@description('Global Standard deployment SKU.')
param deploymentSku string

@description('Global Standard capacity in thousands of tokens per minute.')
param capacity int

@description('AppOnboard session tags.')
param tags object

resource account 'Microsoft.CognitiveServices/accounts@2026-09-01' = {
  name: accountName
  location: location
  kind: 'OpenAI'
  tags: tags
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: deploymentName
  sku: {
    name: deploymentSku
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output resourceId string = account.id
output endpoint string = 'https://${account.name}.cognitiveservices.azure.com/'
