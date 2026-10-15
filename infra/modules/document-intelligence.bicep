@description('Azure region for the Document Intelligence account.')
param location string

@description('Exact globally unique account name.')
param accountName string

@description('AppOnboard session tags.')
param tags object

resource account 'Microsoft.CognitiveServices/accounts@2026-09-01' = {
  name: accountName
  location: location
  kind: 'FormRecognizer'
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

output resourceId string = account.id
output endpoint string = 'https://${account.name}.cognitiveservices.azure.com/'
