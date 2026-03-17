import { INode, INodeData, INodeParams, ICommonObject } from 'flowise-components';
import { authenticatedFetch } from './utils';

class AgenTruxListEvents implements INode {
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
        this.label = 'AgenTrux List Events';
        this.name = 'authPubSubListEvents';
        this.version = 1.0;
        this.description = 'List events from an AgenTrux topic with pagination and filtering';
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
                label: 'Limit',
                name: 'limit',
                type: 'number',
                default: 50,
                optional: true,
                description: 'Maximum number of events to return (1-100)',
            },
            {
                label: 'Event Type Filter',
                name: 'eventType',
                type: 'string',
                optional: true,
                description: 'Filter events by type (optional)',
            },
            {
                label: 'Cursor',
                name: 'cursor',
                type: 'string',
                optional: true,
                description: 'Pagination cursor from a previous response',
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
        const limit = (inputParams.limit as number) || 50;
        const eventType = (inputParams.eventType as string) || '';
        const cursor = (inputParams.cursor as string) || '';

        if (!topicId) {
            throw new Error('Topic ID is required');
        }

        let path = `/topics/${topicId}/events?limit=${limit}`;
        if (cursor) path += `&cursor=${encodeURIComponent(cursor)}`;
        if (eventType) path += `&type=${encodeURIComponent(eventType)}`;

        const result = await authenticatedFetch(
            baseUrl,
            scriptId,
            secret,
            'GET',
            path,
            undefined,
            grantToken,
        );

        // Return JSON string of the full response (events + next_cursor)
        return JSON.stringify(result);
    }
}

module.exports = { nodeClass: AgenTruxListEvents };
