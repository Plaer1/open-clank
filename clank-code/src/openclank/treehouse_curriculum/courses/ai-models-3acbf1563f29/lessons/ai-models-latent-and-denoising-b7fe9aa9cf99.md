# Compare generation mechanisms with small traces

Three independent supplied toy records:

- GAN: discriminator score for a generated sample changes 0.2→0.7 after an unspecified generator update. A better score against that discriminator is not independent quality proof.
- VAE illustration: decoder d(z)=2z+1; encoded z=0 gives reconstruction value 1. A separately supplied sampled z=1 gives 3.
- Denoising illustration: start 9; predicted noise values 2 then 1; subtract each in order: 9→7→6. Change the first prediction to 3.

These show an adversarial score, reconstruction versus sampling, and iterative denoising. Scalar arithmetic is not a full VAE or diffusion schedule and does not establish learned model quality.
