// read this to see how one person has configured the SQS Pipeline.
// For my particular sqs queue the url is in env as SQS_QUEUE_URL


'use strict';
require('dotenv').config();

process.on("uncaughtException", (err) => {
    console.error("Uncaught Exception:", err);
    process.exit(1);
});

process.on("unhandledRejection", (reason) => {
    console.error("Unhandled Rejection:", reason);
    process.exit(1);
});

const {
    SQSClient,
    ReceiveMessageCommand,
    DeleteMessageCommand,
    ChangeMessageVisibilityCommand,
} = require("@aws-sdk/client-sqs");
const moment = require("moment-timezone");
const { logger } = require("./Logs/myLogger");
const { handleNewTicketSla } = require("./services/slaWorker/handlers/newTicket");
const { handleReopenSla } = require("./services/slaWorker/handlers/reopen");
const { createJobExecution, createOrUpdateFailedJob } = require("./dbquery/jobExecution.query");
console.log("server reached");

// ── Config ────────────────────────────────────────────────────────────────────
const SLA_QUEUE_URL = process.env.SLA_QUEUE_URL;
const MAX_MESSAGES = 10;
const POLL_WAIT_SECONDS = 20;  // long-poll — blocks up to 20s if queue is empty
const VISIBILITY_TIMEOUT = 60;  // seconds — SLA calc finishes well within this
const CONCURRENCY_LIMIT = 5;   // max parallel jobs per batch

const sqsClient = new SQSClient({ region: process.env.AWS_DEFAULT_REGION });



let shuttingDown = false;

process.on("SIGTERM", () => { shuttingDown = true; });
process.on("SIGINT", () => { shuttingDown = true; });


// ── Entry point ───────────────────────────────────────────────────────────────
async function runSlaWorker() {
    console.log("Entered runSlaWorker");

    if (!SLA_QUEUE_URL) {
        console.log("SLA_QUEUE_URL:", SLA_QUEUE_URL);
        logger.warn("SLA_QUEUE_URL not set — SLA worker will not start");
        return;
    }

    console.log("Before while loop");

    while (!shuttingDown) {
        console.log("Polling...");
        try {
            await processBatch();
        } catch (err) {
            console.error("processBatch error", err);
            await sleep(10000);
        }
    }

    console.log("Exited while loop");
}


// ── Batch processor ───────────────────────────────────────────────────────────
async function processBatch() {
    const res = await sqsClient.send(new ReceiveMessageCommand({
        QueueUrl: SLA_QUEUE_URL,
        MaxNumberOfMessages: MAX_MESSAGES,
        WaitTimeSeconds: POLL_WAIT_SECONDS,
        VisibilityTimeout: VISIBILITY_TIMEOUT,
        AttributeNames: ["ApproximateReceiveCount"],
    }));

    const messages = res.Messages || [];
    if (messages.length === 0) return;

    logger.info("SLA worker: received batch", { count: messages.length });

    // Semaphore — cap parallel in-flight jobs at CONCURRENCY_LIMIT.
    const semaphore = [];

    for (const sqsMsg of messages) {
        if (semaphore.length >= CONCURRENCY_LIMIT) {
            await Promise.race(semaphore);
        }
        const task = (async () => {
            // ── Parse ─────────────────────────────────────────────────────────
            let payload;
            try {
                payload = JSON.parse(sqsMsg.Body);
                console.log("Payload: ", payload)
                console.log("attempt number: ", sqsMsg.Attributes?.ApproximateReceiveCount);
                const dataToSend = {
                    rootMessageId: payload.messageId,
                    blockId: `Ticket_${payload.ticketId}`,
                    entityType: payload.entityType,
                    jobType: payload.jobType,
                    status: "Processing",
                    startedAt: moment.utc().format("YYYY-MM-DD HH:mm:ss"),
                    // completedAt: moment().tz("Asia/Kolkata").format("YYYY-MM-DD HH:mm:ss"),
                    attemptNumber: sqsMsg.Attributes?.ApproximateReceiveCount,
                }
                await createJobExecution(dataToSend);
                //
            } catch (parseErr) {
                // Malformed message — do NOT delete it, let visibility expire → DLQ.
                console.log("processBatch: error: ", parseErr)
                logger.error("SLA worker: could not parse message body — leaving for DLQ", {
                    messageId: sqsMsg.MessageId,
                    error: parseErr.message,
                });
                return;
            }

            const jobLogger = logger.child({ slaJobType: payload.type, ticketId: payload.ticketId });
            const stopExtender = startVisibilityExtender(sqsMsg.ReceiptHandle, jobLogger);
            // ── Dispatch ──────────────────────────────────────────────────────
            try {
                if (payload.jobType === "Sla_Calculation") {
                    await handleNewTicketSla(payload, jobLogger);

                } else if (payload.jobType === "Sla_Updation") {
                    await handleReopenSla(payload, jobLogger);

                } else {
                    // Unknown type — discard so it doesn't clog the queue.
                    jobLogger.warn("SLA worker: unknown job type — discarding", { type: payload.type });
                }

                // Job done — remove from queue.
                stopExtender();
                //  let  payload = JSON.parse(sqsMsg.Body);

                const dataToSend = {
                    rootMessageId: payload.messageId,
                    blockId: `Ticket_${payload.ticketId}`,
                    entityType: payload.entityType,
                    jobType: payload.jobType,
                    status: "Success",
                    // startedAt: moment().tz("Asia/Kolkata").format("YYYY-MM-DD HH:mm:ss"),
                    completedAt: moment.utc().format("YYYY-MM-DD HH:mm:ss"),
                    attemptNumber: sqsMsg.Attributes?.ApproximateReceiveCount,
                }
                await createJobExecution(dataToSend);
                //


                await deleteMessage(sqsMsg.ReceiptHandle);
                jobLogger.info("SLA worker: job completed");

            } catch (jobErr) {
                // Handler failed — do NOT delete. SQS will retry after visibility
                // timeout expires. After maxReceiveCount (3) failures it goes to DLQ.

                stopExtender();

                await createOrUpdateFailedJob({
                    rootMessageId: payload.messageId,
                    blockId: `Ticket_${payload.ticketId}`,
                    entityType: payload.entityType,
                    jobType: payload.jobType,
                    completedAt: moment.utc().format("YYYY-MM-DD HH:mm:ss"),
                    errorMessage: jobErr.message,
                    attemptNumber: sqsMsg.Attributes?.ApproximateReceiveCount
                });

                jobLogger.error("SLA worker: job failed — releasing for SQS retry", {
                    error: jobErr.message,
                    stack: jobErr.stack,
                });
            }
        })();

        semaphore.push(task);
        task.finally(() => {
            const idx = semaphore.indexOf(task);
            if (idx !== -1) semaphore.splice(idx, 1);
        });
    }

    // Wait for all in-flight tasks to settle before polling the next batch.
    await Promise.allSettled(semaphore);
}

// ── Visibility extender ───────────────────────────────────────────────────────
// Keeps the message invisible to other consumers while we are processing it.
function startVisibilityExtender(receiptHandle, jobLogger) {
    const interval = setInterval(async () => {
        try {
            await sqsClient.send(new ChangeMessageVisibilityCommand({
                QueueUrl: SLA_QUEUE_URL,
                ReceiptHandle: receiptHandle,
                VisibilityTimeout: VISIBILITY_TIMEOUT,
            }));
        } catch (err) {
            jobLogger.warn("SLA worker: failed to extend visibility", { error: err.message });
        }
    }, (VISIBILITY_TIMEOUT - 10) * 1000); // extend every 50s for a 60s window

    return () => clearInterval(interval); // caller calls this as stopExtender()
}


// ── Helpers ───────────────────────────────────────────────────────────────────
async function deleteMessage(receiptHandle) {
    await sqsClient.send(new DeleteMessageCommand({
        QueueUrl: SLA_QUEUE_URL,
        ReceiptHandle: receiptHandle,
    }));
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}


runSlaWorker().catch((err) => {
    logger.error("Fatal SLA worker error", {
        error: err.message,
        stack: err.stack,
    });
    process.exit(1);
});

