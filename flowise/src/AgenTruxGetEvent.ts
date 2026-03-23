import { INode, INodeData, INodeParams, ICommonObject } from 'flowise-components';
import { authenticatedFetch } from './utils';

class AgenTruxGetEvent implements INode {
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
        this.label = 'AgenTrux Get Event';
        this.name = 'authPubSubGetEvent';
        this.version = 1.0;
        this.description = 'Get a single event by ID from an AgenTrux topic';
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
                label: 'Event ID',
                name: 'eventId',
                type: 'string',
                description: 'UUID of the event to retrieve',
            },
        ];
    }

    async run(nodeData: INodeData): Promise<string> {
        const credentialData = nodeData.credential as unknown as ICommonObject;
        const inputParams = nodeData.inputs as ICommonObject;

        const baseUrl = (credentialData.baseUrl as string).replace(/\/+$/, '');
        const scriptId = credentialData.scriptId as string;
        const clientSecret = credentialData.clientSecret as string;
        const inviteCode = (credentialData.inviteCode as string) || undefined;

        const topicId = inputParams.topicId as string;
        const eventId = inputParams.eventId as string;

        if (!topicId || !eventId) {
            throw new Error('Topic ID and Event ID are required');
        }

        const result = await authenticatedFetch(
            baseUrl,
            scriptId,
            clientSecret,
            'GET',
            `/topics/${topicId}/events/${eventId}`,
            undefined,
            inviteCode,
        );

        // Return single event object as JSON string
        return JSON.stringify(result);
    }
}

module.exports = { nodeClass: AgenTruxGetEvent };
