import { INode, INodeData, INodeParams, ICommonObject } from 'flowise-components';
import { getValidToken, authenticatedFetch } from './utils';

class AgenTruxPublish implements INode {
    label: string;
    name: string;
    version: number;
    description: string;
    type: string;
    icon: string;
    category: string;
    baseClasses: string[];
    credential: INodeParams;
    inputs: INodeParams[];

    constructor() {
        this.label = 'AgenTrux Publish';
        this.name = 'authPubSubPublish';
        this.version = 1.0;
        this.description = 'Publish an event to an AgenTrux topic';
        this.type = 'action';
        this.icon = 'agentrux.svg';
        this.category = 'AgenTrux';
        this.baseClasses = [this.type];
        this.credential = {
            label: 'AgenTrux Credential',
            name: 'credential',
            type: 'credential',
            credentialNames: ['authPubSubApi'],
        };
        this.inputs = [
            {
                label: 'Topic ID',
                name: 'topicId',
                type: 'string',
                description: 'UUID of the target topic',
            },
            {
                label: 'Event Type',
                name: 'eventType',
                type: 'string',
                description: 'Type identifier for the event',
            },
            {
                label: 'Payload',
                name: 'payload',
                type: 'json',
                description: 'JSON payload for the event',
                default: '{}',
            },
            {
                label: 'Input',
                name: 'input',
                type: 'string',
                optional: true,
                description: 'Optional text input to include in payload as "text" field',
            },
        ];
    }

    async run(nodeData: INodeData): Promise<string> {
        const credentialData = nodeData.credential as unknown as ICommonObject;
        const inputParams = nodeData.inputs as ICommonObject;

        const baseUrl = (credentialData.baseUrl as string).replace(/\/+$/, '');
        const scriptId = credentialData.scriptId as string;
        const secret = credentialData.secret as string;
        const grantToken = (credentialData.grantToken as string) || undefined;

        const topicId = inputParams.topicId as string;
        const eventType = inputParams.eventType as string;
        const input = inputParams.input as string | undefined;

        let payload: Record<string, unknown>;
        const rawPayload = inputParams.payload;
        if (typeof rawPayload === 'string') {
            try {
                payload = JSON.parse(rawPayload);
            } catch {
                payload = { raw: rawPayload };
            }
        } else {
            payload = rawPayload as Record<string, unknown> || {};
        }

        // Merge optional text input
        if (input) {
            payload.text = input;
        }

        if (!topicId || !eventType) {
            throw new Error('Topic ID and Event Type are required');
        }

        const token = await getValidToken(baseUrl, scriptId, secret, grantToken);

        const result = await authenticatedFetch(
            baseUrl,
            scriptId,
            secret,
            'POST',
            `/topics/${topicId}/events`,
            { type: eventType, payload },
            grantToken,
        );

        // Return event_id as string (Flowise nodes return string)
        return result.event_id as string;
    }
}

module.exports = { nodeClass: AgenTruxPublish };
