@description('Verified Microsoft Entra object ID of the current operator.')
param deployerObjectId string

@description('Resource ID of the Document Intelligence account.')
param documentIntelligenceAccountName string

@description('Resource ID of the Azure OpenAI account.')
param openAiAccountName string

var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource documentIntelligenceAccount 'Microsoft.CognitiveServices/accounts@2026-09-01' existing = {
  name: documentIntelligenceAccountName
}

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2026-09-01' existing = {
  name: openAiAccountName
}

resource documentIntelligenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(documentIntelligenceAccount.id, deployerObjectId, cognitiveServicesUserRoleId)
  scope: documentIntelligenceAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource openAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, deployerObjectId, cognitiveServicesOpenAiUserRoleId)
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: deployerObjectId
    principalType: 'User'
  }
}
