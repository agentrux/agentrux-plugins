import { INode, INodeData, INodeParams, ICommonObject } from 'flowise-components';
import { authenticatedFetch } from './utils';

class AgenTruxUploadDownload implements INode {
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
        this.label = 'AgenTrux Payload';
        this.name = 'authPubSubUploadDownload';
        this.version = 1.0;
        this.description = 'Upload or download payloads via AgenTrux presigned URLs';
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
                label: 'Operation',
                name: 'operation',
                type: 'options',
                options: [
                    { label: 'Upload', name: 'upload' },
                    { label: 'Download', name: 'download' },
                ],
                default: 'upload',
                description: 'Whether to upload or download a payload',
            },
            {
                label: 'Topic ID',
                name: 'topicId',
                type: 'string',
                description: 'UUID of the target topic',
            },
            // Upload fields
            {
                label: 'Content Type',
                name: 'contentType',
                type: 'string',
                default: 'application/octet-stream',
                optional: true,
                description: 'MIME type of the payload to upload',
            },
            {
                label: 'Upload Data',
                name: 'uploadData',
                type: 'string',
                optional: true,
                description: 'Text or base64-encoded data to upload',
            },
            // Download fields
            {
                label: 'Object ID',
                name: 'objectId',
                type: 'string',
                optional: true,
                description: 'UUID of the payload object to download',
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

        const operation = inputParams.operation as string;
        const topicId = inputParams.topicId as string;

        if (!topicId) {
            throw new Error('Topic ID is required');
        }

        if (operation === 'upload') {
            const contentType = (inputParams.contentType as string) || 'application/octet-stream';
            const uploadData = inputParams.uploadData as string;

            if (!uploadData) {
                throw new Error('Upload Data is required for upload operation');
            }

            // Step 1: Get presigned upload URL from AgenTrux
            const createResult = await authenticatedFetch(
                baseUrl,
                scriptId,
                secret,
                'POST',
                `/topics/${topicId}/payloads`,
                { content_type: contentType },
                grantToken,
            );

            const uploadUrl: string = createResult.upload_url;
            const objectId: string = createResult.object_id;

            // Step 2: Upload data to presigned URL
            const isBase64 = /^[A-Za-z0-9+/]+=*$/.test(uploadData) && uploadData.length > 100;
            const body = isBase64 ? Buffer.from(uploadData, 'base64') : uploadData;

            const uploadResp = await fetch(uploadUrl, {
                method: 'PUT',
                headers: { 'Content-Type': contentType },
                body,
            });

            if (!uploadResp.ok) {
                throw new Error(`Upload failed with status ${uploadResp.status}: ${await uploadResp.text()}`);
            }

            return JSON.stringify({
                object_id: objectId,
                status: 'uploaded',
                content_type: contentType,
            });

        } else if (operation === 'download') {
            const objectId = inputParams.objectId as string;

            if (!objectId) {
                throw new Error('Object ID is required for download operation');
            }

            // Step 1: Get presigned download URL from AgenTrux
            const result = await authenticatedFetch(
                baseUrl,
                scriptId,
                secret,
                'GET',
                `/topics/${topicId}/payloads/${objectId}`,
                undefined,
                grantToken,
            );

            const downloadUrl: string = result.download_url;

            // Step 2: Download from presigned URL
            const downloadResp = await fetch(downloadUrl);
            if (!downloadResp.ok) {
                throw new Error(`Download failed with status ${downloadResp.status}: ${await downloadResp.text()}`);
            }

            const contentType = downloadResp.headers.get('content-type') || 'application/octet-stream';
            const arrayBuffer = await downloadResp.arrayBuffer();
            const buffer = Buffer.from(arrayBuffer);

            // For text content types, return as string; for binary, return base64
            if (contentType.startsWith('text/') || contentType.includes('json')) {
                return buffer.toString('utf-8');
            } else {
                return JSON.stringify({
                    object_id: objectId,
                    content_type: contentType,
                    data_base64: buffer.toString('base64'),
                    size_bytes: buffer.length,
                });
            }

        } else {
            throw new Error(`Unknown operation: ${operation}`);
        }
    }
}

module.exports = { nodeClass: AgenTruxUploadDownload };
