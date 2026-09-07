/**
 * The server's types, not a hand-written copy of them.
 *
 * `schema.d.ts` is generated from the FastAPI schema by `make openapi`, and CI fails if
 * the checked-in copy has drifted. Aliasing the pieces the app uses here is what makes
 * that check bite: rename a field on the server and this file stops compiling, instead
 * of the UI quietly rendering `undefined`.
 */

import type { components } from '@/api/schema'

export type SessionResponse = components['schemas']['SessionResponse']
export type CurrentUser = components['schemas']['UserSummary']
export type OrganizationSummary = components['schemas']['OrganizationSummary']
export type LoginRequest = components['schemas']['LoginRequest']
export type PasswordChangeRequest = components['schemas']['PasswordChangeRequest']

export type OrganizationResponse = components['schemas']['OrganizationResponse']
export type OrganizationCreateRequest = components['schemas']['OrganizationCreateRequest']
export type OrganizationUpdateRequest = components['schemas']['OrganizationUpdateRequest']
export type MemberResponse = components['schemas']['MemberResponse']
export type MemberUpdateRequest = components['schemas']['MemberUpdateRequest']
export type InvitationResponse = components['schemas']['InvitationResponse']
export type InvitationCreateRequest = components['schemas']['InvitationCreateRequest']
export type IssuedInvitationResponse = components['schemas']['IssuedInvitationResponse']
export type InvitationPreviewResponse = components['schemas']['InvitationPreviewResponse']
export type InvitationAcceptRequest = components['schemas']['InvitationAcceptRequest']

export type ModelResponse = components['schemas']['ModelResponse']
export type ModelCreateRequest = components['schemas']['ModelCreateRequest']
export type ModelUpdateRequest = components['schemas']['ModelUpdateRequest']
export type ModelTestRequest = components['schemas']['ModelTestRequest']
export type CredentialStatus = components['schemas']['CredentialStatus']
export type ProbeResponse = components['schemas']['ProbeResponse']

export type GatewayResponse = components['schemas']['GatewayResponse']
export type GatewayCreateRequest = components['schemas']['GatewayCreateRequest']
export type GatewayUpdateRequest = components['schemas']['GatewayUpdateRequest']
export type GatewayTestRequest = components['schemas']['GatewayTestRequest']
export type GatewayTestResponse = components['schemas']['GatewayTestResponse']
export type TargetSummary = components['schemas']['TargetSummary']
export type GatewayTargetRequest = components['schemas']['GatewayTargetRequest']
export type AttemptResponse = components['schemas']['AttemptResponse']
export type MemoryConfig = components['schemas']['MemoryConfig']
export type LoggingConfig = components['schemas']['LoggingConfig']
export type LimitsConfig = components['schemas']['LimitsConfig']
export type ApiKeyResponse = components['schemas']['ApiKeyResponse']
export type ApiKeyCreateRequest = components['schemas']['ApiKeyCreateRequest']
export type IssuedApiKeyResponse = components['schemas']['IssuedApiKeyResponse']

export type SummaryResponse = components['schemas']['SummaryResponse']
export type SeriesResponse = components['schemas']['SeriesResponse']
export type BucketResponse = components['schemas']['BucketResponse']
export type PercentilesResponse = components['schemas']['PercentilesResponse']
export type ModelTrafficResponse = components['schemas']['ModelTrafficResponse']
export type ErrorGroupResponse = components['schemas']['ErrorGroupResponse']
export type RequestLogResponse = components['schemas']['RequestLogResponse']
export type RequestDetailResponse = components['schemas']['RequestDetailResponse']
export type TranscriptResponse = components['schemas']['TranscriptResponse']

export type OrganizationPage = components['schemas']['Page_OrganizationResponse_']
export type MemberPage = components['schemas']['Page_MemberResponse_']
export type InvitationPage = components['schemas']['Page_InvitationResponse_']
export type ModelPage = components['schemas']['Page_ModelResponse_']
export type GatewayPage = components['schemas']['Page_GatewayResponse_']
export type RequestLogPage = components['schemas']['Page_RequestLogResponse_']
