class CreateProjectResponse:
    def __init__(
        self,
        apiProjectId,
        uploadUrl,
        uploadMethod,
        objectPath,
        bucket,
        expiresAt,
    ):
        self.apiProjectId = apiProjectId
        self.uploadUrl = uploadUrl
        self.uploadMethod = uploadMethod
        self.objectPath = objectPath
        self.bucket = bucket
        self.expiresAt = expiresAt

    @classmethod
    def from_dict(cls, data):
        return cls(
            apiProjectId=data["apiProjectId"],
            uploadUrl=data["uploadUrl"],
            uploadMethod=data.get("uploadMethod") or "PUT",
            objectPath=data["objectPath"],
            bucket=data["bucket"],
            expiresAt=data["expiresAt"],
        )


class ProjectSummary:
    def __init__(
        self,
        apiProjectId,
        status,
        originalFileName,
        contentType,
        hasVideo,
        generationCount,
        createdAt,
        updatedAt,
    ):
        self.apiProjectId = apiProjectId
        self.status = status
        self.originalFileName = originalFileName
        self.contentType = contentType
        self.hasVideo = hasVideo
        self.generationCount = generationCount
        self.createdAt = createdAt
        self.updatedAt = updatedAt

    @classmethod
    def from_dict(cls, data):
        return cls(
            apiProjectId=data["apiProjectId"],
            status=data.get("status") or "uploading",
            originalFileName=data.get("originalFileName"),
            contentType=data.get("contentType"),
            hasVideo=bool(data.get("hasVideo")),
            generationCount=int(data.get("generationCount") or 0),
            createdAt=data.get("createdAt"),
            updatedAt=data.get("updatedAt"),
        )


class ListProjectsResponse:
    def __init__(self, projects):
        self.projects = projects

    @classmethod
    def from_dict(cls, data):
        items = data.get("projects")
        if not isinstance(items, list):
            items = []
        projects = [ProjectSummary.from_dict(item) for item in items if isinstance(item, dict)]
        return cls(projects=projects)


class CreateGenerationResponse:
    def __init__(self, generationId, apiProjectId, modelKey, status):
        self.generationId = generationId
        self.apiProjectId = apiProjectId
        self.modelKey = modelKey
        self.status = status

    @classmethod
    def from_dict(cls, data):
        return cls(
            generationId=data["generationId"],
            apiProjectId=data["apiProjectId"],
            modelKey=data["modelKey"],
            status=data["status"],
        )


class ProjectGenerationSummary:
    def __init__(self, generationId, status, modelKey, progress, createdAt, updatedAt):
        self.generationId = generationId
        self.status = status
        self.modelKey = modelKey
        self.progress = progress
        self.createdAt = createdAt
        self.updatedAt = updatedAt

    @classmethod
    def from_dict(cls, data):
        progress = data.get("progress")
        return cls(
            generationId=data["generationId"],
            status=data.get("status") or "queued",
            modelKey=data.get("modelKey"),
            progress=int(progress) if progress is not None else None,
            createdAt=data.get("createdAt"),
            updatedAt=data.get("updatedAt"),
        )


class ListProjectGenerationsResponse:
    def __init__(self, apiProjectId, generations):
        self.apiProjectId = apiProjectId
        self.generations = generations

    @classmethod
    def from_dict(cls, data):
        items = data.get("generations")
        if not isinstance(items, list):
            items = []
        generations = [ProjectGenerationSummary.from_dict(item) for item in items if isinstance(item, dict)]
        return cls(
            apiProjectId=data["apiProjectId"],
            generations=generations,
        )


class GenerationProgressStage:
    def __init__(
        self,
        jobId,
        jobState,
        status,
        progress,
        statusMessage,
        error,
        errorDetails,
    ):
        self.jobId = jobId
        self.jobState = jobState
        self.status = status
        self.progress = progress
        self.statusMessage = statusMessage
        self.error = error
        self.errorDetails = errorDetails

    @classmethod
    def from_dict(cls, data):
        return cls(
            jobId=data.get("jobId"),
            jobState=data.get("jobState"),
            status=data.get("status") or "pending",
            progress=int(data.get("progress") or 0),
            statusMessage=data.get("statusMessage"),
            error=data.get("error"),
            errorDetails=data.get("errorDetails"),
        )


class GenerationProgressResponse:
    def __init__(
        self,
        generationId,
        apiProjectId,
        status,
        hasVideo,
        preProcessing,
        inference,
        restoredVideo,
        error,
        errorDetails,
    ):
        self.generationId = generationId
        self.apiProjectId = apiProjectId
        self.status = status
        self.hasVideo = hasVideo
        self.preProcessing = preProcessing
        self.inference = inference
        self.restoredVideo = restoredVideo
        self.error = error
        self.errorDetails = errorDetails

    @classmethod
    def from_dict(cls, data):
        restored_video = data.get("restoredVideo")
        return cls(
            generationId=data["generationId"],
            apiProjectId=data["apiProjectId"],
            status=data["status"],
            hasVideo=bool(data.get("hasVideo")),
            preProcessing=GenerationProgressStage.from_dict(data["preProcessing"]),
            inference=GenerationProgressStage.from_dict(data["inference"]),
            restoredVideo=(GenerationProgressStage.from_dict(restored_video) if restored_video else None),
            error=data.get("error"),
            errorDetails=data.get("errorDetails"),
        )


class GenerationDownloadResponse:
    def __init__(
        self,
        generationId,
        apiProjectId,
        downloadType,
        downloadUrl,
        fileName,
        storagePath,
        bucket,
        mimeType,
    ):
        self.generationId = generationId
        self.apiProjectId = apiProjectId
        self.downloadType = downloadType
        self.downloadUrl = downloadUrl
        self.fileName = fileName
        self.storagePath = storagePath
        self.bucket = bucket
        self.mimeType = mimeType

    @classmethod
    def from_dict(cls, data):
        return cls(
            generationId=data["generationId"],
            apiProjectId=data["apiProjectId"],
            downloadType=data["downloadType"],
            downloadUrl=data["downloadUrl"],
            fileName=data["fileName"],
            storagePath=data["storagePath"],
            bucket=data["bucket"],
            mimeType=data["mimeType"],
        )


class WebhookTestEventResponse:
    def __init__(self, svixMessageId, eventId, eventType, mode, apiKeyId=None):
        self.svixMessageId = svixMessageId
        self.eventId = eventId
        self.eventType = eventType
        self.mode = mode
        self.apiKeyId = apiKeyId

    @classmethod
    def from_dict(cls, data):
        return cls(
            svixMessageId=data["svixMessageId"],
            eventId=data["eventId"],
            eventType=data["eventType"],
            mode=data.get("mode"),
            apiKeyId=data.get("apiKeyId"),
        )


class GenerationWebhookEvent:
    def __init__(
        self,
        eventType,
        eventId,
        createdAt,
        apiKeyId,
        apiProjectId,
        generationId,
        status,
        hasVideo,
        modelKey,
        error,
        errorDetails,
    ):
        self.eventType = eventType
        self.eventId = eventId
        self.createdAt = createdAt
        self.apiKeyId = apiKeyId
        self.apiProjectId = apiProjectId
        self.generationId = generationId
        self.status = status
        self.hasVideo = hasVideo
        self.modelKey = modelKey
        self.error = error
        self.errorDetails = errorDetails

    @classmethod
    def from_dict(cls, data):
        return cls(
            eventType=data["eventType"],
            eventId=data["eventId"],
            createdAt=data["createdAt"],
            apiKeyId=data["apiKeyId"],
            apiProjectId=data.get("apiProjectId"),
            generationId=data["generationId"],
            status=data["status"],
            hasVideo=data.get("hasVideo"),
            modelKey=data.get("modelKey"),
            error=data.get("error"),
            errorDetails=data.get("errorDetails"),
        )


class AudioIsolationResult:
    def __init__(self, project, generation):
        self.project = project
        self.generation = generation


ModelKey = ("diffio-2", "diffio-2-flash", "diffio-3")
DownloadType = ("audio", "mp3", "video")
WebhookMode = ("test", "live")
WebhookEventType = (
    "generation.queued",
    "generation.processing",
    "generation.failed",
    "generation.completed",
)
GenerationWebhookStatus = ("queued", "processing", "error", "complete")
