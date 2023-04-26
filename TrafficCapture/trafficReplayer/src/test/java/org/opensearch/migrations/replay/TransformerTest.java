package org.opensearch.migrations.replay;

import lombok.extern.slf4j.Slf4j;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;
import org.opensearch.migrations.replay.datahandlers.HttpMessageTransformerHandler;
import org.opensearch.migrations.replay.datahandlers.IPacketToHttpHandler;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Random;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BiFunction;
import java.util.function.Function;
import java.util.stream.Collector;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

@Slf4j
public class TransformerTest {

    @Test
    public void testTransformer() throws Exception {
        var referenceStringBuilder = new StringBuilder();
        var numFinalizations = new AtomicInteger();
        // mock object.  values don't matter at all - not what we're testing
        final var dummyAggregatedResponse = new AggregatedRawResponse(17, null, null);
        AtomicInteger decayedMilliseconds = new AtomicInteger(50);
        final int DECAY_FACTOR = 4;
        var transformingHandler = new HttpMessageTransformerHandler(
                new IPacketToHttpHandler() {
                    ByteArrayOutputStream byteArrayOutputStream = new ByteArrayOutputStream();
                    @Override
                    public CompletableFuture<Void> consumeBytes(byte[] nextRequestPacket) {
                        return CompletableFuture.runAsync(() -> {
                            try {
                                log.info("Consuming bytes: "+new String(nextRequestPacket, StandardCharsets.UTF_8));
                                int oldV = decayedMilliseconds.get();
                                int v = oldV / DECAY_FACTOR;
                                Assertions.assertTrue(decayedMilliseconds.compareAndSet(oldV, v));
                                Thread.sleep(decayedMilliseconds.get());
                            } catch (InterruptedException e) {
                                throw new RuntimeException(e);
                            }
                            try {
                                byteArrayOutputStream.write(nextRequestPacket);
                            } catch (IOException e) {
                                throw new RuntimeException(e);
                            }
                        });
                    }

                    @Override
                    public CompletableFuture<AggregatedRawResponse> finalizeRequest() {
                        numFinalizations.incrementAndGet();
                        var bytes = byteArrayOutputStream.toByteArray();
                        Assertions.assertEquals(referenceStringBuilder.toString(), new String(bytes, StandardCharsets.UTF_8));
                        return CompletableFuture.completedFuture(dummyAggregatedResponse);
                    }
                });

        Random r = new Random(2);

        var allConsumesFuture = IntStream.range(0, 3).mapToObj(i->makeRandomString(r)).map(o->(String)o)
                .collect(foldLeft(CompletableFuture.completedFuture(null),
                        (cf, s)->cf.thenApply(v->writeStringToBoth(s, referenceStringBuilder, transformingHandler))));

        var innermostFinalizeCallCount = new AtomicInteger();
        var finalizationFuture = allConsumesFuture.thenCompose(v->transformingHandler.finalizeRequest());
        finalizationFuture.whenComplete((arr,t)->{
            Assertions.assertNull(t);
            Assertions.assertNotNull(arr);
            // do nothing but check connectivity between the layers in the bottom most handler
            innermostFinalizeCallCount.incrementAndGet();
            Assertions.assertEquals(dummyAggregatedResponse, arr);
        });
        finalizationFuture.get();
        Assertions.assertEquals(1, innermostFinalizeCallCount.get());
        Assertions.assertEquals(1, numFinalizations.get());
    }

    public static <A, B> Collector<A, ?, B> foldLeft(final B init, final BiFunction<? super B, ? super A, ? extends B> f) {
        return Collectors.collectingAndThen(
                Collectors.reducing(Function.<B>identity(), a -> b -> f.apply(b, a), Function::andThen),
                endo -> endo.apply(init)
        );
    }
    private static String makeRandomString(Random r) {
        return r.ints(r.nextInt(10), 'A', 'Z')
                .collect(StringBuilder::new, StringBuilder::appendCodePoint, StringBuilder::append)
                .toString();
    }

    private static CompletableFuture<Void> writeStringToBoth(String s, StringBuilder referenceStringBuilder,
                                                             HttpMessageTransformerHandler transformingHandler) {
        log.info("Sending string to transformer: "+s);
        referenceStringBuilder.append(s);
        var bytes = s.getBytes(StandardCharsets.UTF_8);
        return transformingHandler.consumeBytes(bytes);
    }
}
