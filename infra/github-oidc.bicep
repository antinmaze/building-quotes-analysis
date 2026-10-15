targetScope = 'resourceGroup'

param location string = resourceGroup().location
param identityName string = 'id-devis-github'
param githubRepository string
param githubEnvironment string = 'azure-pilot'
@description('Only this operator can receive the two allowed data-plane roles.')
param operatorObjectId string
param createdAt string

var cognitiveContributor = '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68'
var rbacAdministrator = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
var roleConditionTemplate = '''
(
  (!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'}))
  OR (
    @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {a97b65f3-24c7-4388-baec-2e87135dc908, 5e0bd9bd-7b93-4f28-af87-19fc36ad61bd}
    AND @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {__OPERATOR_OBJECT_ID__}
  )
)
AND
(
  (!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'}))
  OR (
    @Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {a97b65f3-24c7-4388-baec-2e87135dc908, 5e0bd9bd-7b93-4f28-af87-19fc36ad61bd}
    AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {__OPERATOR_OBJECT_ID__}
  )
)
'''
var roleCondition = replace(roleConditionTemplate, '__OPERATOR_OBJECT_ID__', operatorObjectId)

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
  tags: {
    project: 'devis-mngt'
    purpose: 'github-oidc'
    'created-at': createdAt
  }
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: identity
  name: 'github-azure-pilot'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepository}:environment:${githubEnvironment}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource contributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, cognitiveContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource restrictedRbac 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, rbacAdministrator)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rbacAdministrator)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    conditionVersion: '2.0'
    condition: roleCondition
  }
}

output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
