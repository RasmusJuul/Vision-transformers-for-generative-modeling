import numpy as np
import torch
import torch.utils.data
import torchvision.models as models
from torch import nn, optim
from torch.nn import functional as F
from einops import rearrange, repeat
from einops.layers.torch import Rearrange
from pytorch_lightning import LightningModule
from vit_pytorch.vit import Transformer, pair


class Discriminator(nn.Module):
    def __init__(self):
        super(Discriminator, self).__init__()
        ndf = 128
        self.main = nn.Sequential(
            # input is (nc) x 64 x 64
            nn.Conv2d(3, ndf, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf) x 32 x 32
            nn.Conv2d(ndf, ndf * 2, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*2) x 16 x 16
            nn.Conv2d(ndf * 2, ndf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*4) x 16 x 16
            nn.Conv2d(ndf * 2, ndf * 8, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*8) x 8 x 8
            nn.Conv2d(ndf * 4, ndf * 16, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 8),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*16) x 4 x 4
            nn.Conv2d(ndf * 8, 1, 4, 1, 0, bias=False),
            nn.Sigmoid()
        )

    def forward(self, input):
        return self.main(input)
    
class Discriminator_DC(nn.Module):
    def __init__(self, nc=3, ndf=64):
        super(Discriminator_DC, self).__init__()
        self.cv1 = nn.Conv2d(nc, ndf, kernel_size=4, stride=2, padding=1, bias=False) # (3, 64, 64) -> (64, 32, 32)
        self.cv2 = nn.Conv2d(ndf, ndf*2, 4, 2, 1 ) # (64, 32, 32) -> (128, 16, 16)
        self.bn2 = nn.BatchNorm2d(ndf*2) # spatial batch norm is applied on num of channels
        self.cv3 = nn.Conv2d(ndf*2, ndf*4, 4, 2, 1) # (128, 16, 16) -> (256, 8, 8)
        self.bn3 = nn.BatchNorm2d(ndf*4)
        self.cv4 = nn.Conv2d(ndf*4, ndf*8, 4, 2, 1, bias=False) # (256, 8, 8) -> (512, 4, 4)
        self.bn4 = nn.BatchNorm2d(ndf* 8)
        self.cv5 = nn.Conv2d(ndf*8, 1, 4, 1, 0, bias=False) # (512, 4, 4) -> (1, 1, 1)
        self.cv6 = nn.Conv2d(1, 1, 5, 5, 0, bias=False)

    def forward(self, x):
        x = F.leaky_relu(self.cv1(x))
        x = F.leaky_relu(self.bn2(self.cv2(x)), 0.2, True)
        x = F.leaky_relu(self.bn3(self.cv3(x)), 0.2, True)
        x = F.leaky_relu(self.bn4(self.cv4(x)), 0.2, True)
        x = F.leaky_relu(self.cv5(x), 0.2, True)
        x = torch.sigmoid(self.cv6(x))
        return x.view(-1, 1).squeeze(1)

class Discriminator_Patch(nn.Module):
    def __init__(self, in_channels=3):
        super(Discriminator_Patch, self).__init__()

        def discriminator_block(in_filters, out_filters, normalization=True):
            """Returns downsampling layers of each discriminator block"""
            layers = [nn.Conv2d(in_filters, out_filters, 4, stride=2, padding=1)]
            if normalization:
                layers.append(nn.InstanceNorm2d(out_filters))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            return layers

        self.model = nn.Sequential(
            *discriminator_block(in_channels, 64, normalization=False),
            *discriminator_block(64, 128),
            *discriminator_block(128, 256),
            *discriminator_block(256, 512),
            nn.ZeroPad2d((1, 0, 1, 0)),
            nn.Conv2d(in_channels = 512, out_channels = 1, kernel_size = 4, padding=1, bias=False)
        )

    def forward(self, img_A):
        return self.model(img_A)
    
    
##############################
#          PatchGAN          #
##############################

class Generator(nn.Module):
    def __init__(
        self,
        image_size=(128, 128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf=8,
        dropout=0.0,
        emb_dropout=0.0,
    ):
        super().__init__()
        self.image_height, self.image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert (
            self.image_height % patch_height == 0 and self.image_width % patch_width == 0
        ), "Image dimensions must be divisible by the patch size."

        num_patches = (self.image_height // patch_height) * (self.image_width // patch_width)
        patch_dim = (1+channels) * patch_height * patch_width
        self.dim = dim
        self.mean_token = nn.Parameter(torch.randn(1, 1, dim))
        self.log_var_token = nn.Parameter(torch.randn(1, 1, dim))
        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 2, dim))
        self.label_embedding = nn.Linear(num_classes, self.image_height*self.image_width)
        self.decoder_input = nn.Linear(dim + num_classes, dim)
        self.to_patch_embedding = nn.Sequential(
            Rearrange(
                "b c (h p1) (w p2) -> b (h w) (p1 p2 c)",
                p1=patch_height,
                p2=patch_width,
            ),
            nn.Linear(patch_dim, dim),
        )

        self.dropout = nn.Dropout(emb_dropout)

        self.encoder_transformer = Transformer(
            dim, depth, heads, dim_head, mlp_dim, dropout
        )

        self.decoder_conv = nn.Sequential(
            # input is Z, going into a convolution
            nn.ConvTranspose2d(dim, ngf * 16, (4, 4), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 16),
            nn.ReLU(True),
            # state size. (ngf*8) x 4 x 4
            nn.ConvTranspose2d(ngf * 16, ngf * 8, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 8),
            nn.ReLU(True),
            # state size. (ngf*4) x 8 x 8
            nn.ConvTranspose2d(ngf * 8, ngf * 4, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.ReLU(True),
            # state size. (ngf*2) x 16 x 16
            nn.ConvTranspose2d(ngf * 4, ngf * 2, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 2),
            nn.ReLU(True),
            # state size. (ngf) x 32 x 32
            nn.ConvTranspose2d(ngf * 2, ngf, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf),
            nn.ReLU(True),
            # state size. (ngf) x 64 x 64
            nn.ConvTranspose2d(ngf, channels, (4, 4), (2, 2), (1, 1), bias=False),
            nn.Tanh()
            # state size. (nc) x 128 x 128
        )

    def encoder(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        log_var_tokens = repeat(self.log_var_token, "() n d -> b n d", b=b)
        x = torch.cat((log_var_tokens, x), dim=1)
        mean_tokens = repeat(self.mean_token, "() n d -> b n d", b=b)
        x = torch.cat((mean_tokens, x), dim=1)

        x += self.pos_embedding
        x = self.dropout(x)
        x = self.encoder_transformer(x)

        return x

    def decoder(self, z):
        result = self.decoder_input(z)
        result = rearrange(result, "b d -> b d 1 1")
        result = self.decoder_conv(result)
        return result

    def reparameterize(self, mean, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)

        z = eps.mul(std).add_(mean)

        return z

    def forward(self, img, labels):
        label_embeddings = self.label_embedding(labels)
        label_embeddings = label_embeddings.view(-1,self.image_height,self.image_width).unsqueeze(1)
        x = torch.cat([img,label_embeddings],dim = 1)
        x = self.encoder(x)

        mean = x[:, 0]
        log_var = x[:, 1]

        z = self.reparameterize(mean, log_var)
        z = torch.cat([z, labels], dim = 1)
        recons_img = self.decoder(z)

        return recons_img, mean, log_var
    
    def forward_2(self,img,label,num_samples):
        
        
        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        recons_img = self.decoder(z)

        return recons_img
        
    def encoding(self,img,labels):
        label_embeddings = self.label_embedding(labels)
        label_embeddings = label_embeddings.view(-1,self.image_height,self.image_width).unsqueeze(1)
        x = torch.cat([img,label_embeddings],dim = 1)
        x = self.encoder(x)

        mean = x[:, 0]
        log_var = x[:, 1]

        z = self.reparameterize(mean, log_var)
        return z

    


#####################################################################################################


class ViTVAE_GAN(LightningModule):
    def __init__(
        self,
        image_size=(128,128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf = 8,
        dropout=0.0,
        emb_dropout=0.0,
        landa = 100,
        lr=1e-4,
        lr_discriminator = 1e-4,
        frequency_generator = 10,
        frequency_discriminator = 10
        ):
        
        super().__init__()


        self.generator = Generator(image_size=image_size,
                                   patch_size=patch_size,
                                   num_classes=num_classes,
                                   dim=dim,
                                   depth=depth,
                                   heads=heads,
                                   mlp_dim=mlp_dim,
                                   channels=channels,
                                   dim_head=dim_head,
                                   ngf=ngf,
                                   dropout=dropout,
                                   emb_dropout=emb_dropout)
        

        self.discriminator = Discriminator_DC()
        self.landa =landa
        self.lr_discriminator = lr_discriminator
        self.lr = lr
        self.save_hyperparameters()
        self.freq_generator = frequency_generator
        self.freq_discriminator = frequency_discriminator
        self.dim = dim


    def forward(self, img, labels):
        # # Generator
        
        out, mean, log_var = self.generator(img,labels)

        real_label = self.discriminator(img)
        fake_label = self.discriminator(out)

        return out, img, mean, log_var, real_label, fake_label
    


    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        samples = self.generator.decoder(z)
        return samples


    def adversarial_loss(self, y_hat, y):
        loss_object = nn.BCEWithLogitsLoss()
        return {"loss" : loss_object(y_hat, y)}

    def discriminator_loss(self, real_label, fake_label):
        # Loss with the real image
        real_loss = self.adversarial_loss(real_label, torch.ones_like(real_label))
        # Loss with the generated image
        generated_loss = self.adversarial_loss(fake_label, torch.zeros_like(real_label))

        return {"loss_real":real_loss, "loss_fake":generated_loss}

    def generator_loss(self,fake_label, out, img):
        # Want to make the answer of the discriminator all close to zero
        gan_loss = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
        #difference in image 
        #loss_l1 = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        total = gan_loss  #+ (self.landa * loss_l1) 
        print("total loss of generator",total)
        return {"loss":total}  

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}


    def training_step(self, batch, batch_idx, optimizer_idx):
        data, target = batch
        target = target.to(torch.float)

        # train generator
        if optimizer_idx == 0:

            # generate images
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

            loss_dict = self.loss_function(recons_x, x, mu, log_var)
            self.log("Generator Traning Loss - VAE error", loss_dict["loss"])
            return loss_dict["loss"]

        # train generator
        if optimizer_idx == 1:

            # generate images
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

            loss_dict = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
            #self.log("Generator Traning Loss - Generator error",loss_dict)
            self.log("Generator Traning Loss - GAN error", loss_dict["loss"])
            return loss_dict["loss"]

        # train discriminator
        if optimizer_idx == 2:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.adversarial_loss(real_label, torch.ones_like(real_label))
            self.log("Discriminator Traning Loss - Real",loss_dict["loss"])

            return loss_dict["loss"]
        
                # train discriminator
        if optimizer_idx == 3:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))

            self.log("Discriminator Traning Loss - Fake ",loss_dict["loss"])

            return loss_dict["loss"]


    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)

        recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

        loss_Discriminator_real = self.adversarial_loss(real_label, torch.ones_like(real_label))
        self.log("Discriminator loss validation real image", loss_Discriminator_real["loss"])
        loss_Discriminator_fake = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))
        self.log("Discriminator loss validation fake image", loss_Discriminator_fake["loss"])
        loss_Generator = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
        self.log("GAN_loss validation fake image", loss_Generator["loss"])

        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })

    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
        
        loss_Discriminator_real = self.adversarial_loss(real_label, torch.ones_like(real_label))
        self.log("Discriminator loss test real image", loss_Discriminator_real["loss"])
        loss_Discriminator_fake = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))
        self.log("Discriminator loss test fake image", loss_Discriminator_fake["loss"])
        loss_Generator = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
        self.log("GAN_loss test fake image", loss_Generator["loss"])

        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })

    def configure_optimizers(self):
        optimizer1 = optim.AdamW(self.generator.parameters(), lr=self.lr)
        optimizer2 = optim.AdamW(self.discriminator.parameters(), lr = self.lr_discriminator)
        lr_scheduler1 = optim.lr_scheduler.ReduceLROnPlateau(optimizer1, patience=6)
        lr_scheduler_config_1 = {
            "scheduler": lr_scheduler1,
            "interval": "epoch",
            "monitor": "val_loss",}
        lr_scheduler2 = optim.lr_scheduler.ReduceLROnPlateau(optimizer2, patience=6)
        lr_scheduler_config_2 = {
            "scheduler": lr_scheduler2,
            "interval": "epoch",
            "monitor": "val_loss",}
        return [optimizer1, optimizer1, optimizer2, optimizer2], [lr_scheduler_config_1,lr_scheduler_config_2]
        
################################################################################################################################


class ViTVAE_GAN_prepared(LightningModule):
    def __init__(
        self,
        image_size=(128,128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf = 8,
        dropout=0.0,
        emb_dropout=0.0,
        landa = 100,
        lr=1e-4,
        lr_discriminator = 1e-4,
        frequency_generator = 10,
        frequency_discriminator = 10
        ):
        
        super().__init__()


        self.generator = CViTVAE().load_from_checkpoint("/work3/s164564/Vision-transformers-for-generative-modeling/models/CViTVAE2022-04-29-1735/CViTVAE-epoch=174.ckpt")
        

        # For now we will have a normal Discriminator; then I will change it to PatchGAN
        self.discriminator = Discriminator_DC()
        self.landa =landa
        self.lr = lr
        self.lr_discriminator = lr_discriminator
        self.save_hyperparameters()
        self.freq_generator = frequency_generator
        self.freq_discriminator = frequency_discriminator
        self.dim = dim


    def forward(self, img, labels):
        # # Generator
        out = self.generator.forward_2(img,img.shape[0])
        real_label = self.discriminator(img)
        fake_label = self.discriminator(out)

        return out, img, real_label, fake_label
    

    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        samples = self.generator.sample(num_samples=num_samples,label=label)
        return samples


    def adversarial_loss(self, y_hat, y):
        loss_object = nn.BCEWithLogitsLoss()
        return {"loss" : loss_object(y_hat, y)}

    def discriminator_loss(self, real_label, fake_label):
        # Loss with the real image
        real_loss = self.adversarial_loss(real_label, torch.ones_like(real_label))
        # Loss with the generated image
        generated_loss = self.adversarial_loss(fake_label, torch.zeros_like(real_label))

        return {"loss_real":real_loss, "loss_fake":generated_loss}

    def generator_loss(self,fake_label, out, img):
        # Want to make the answer of the discriminator all close to zero
        gan_loss = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
        return {"loss": gan_loss}  

    # def loss_function(self,recons_x, x, mu, log_var):
    #     """
    #     Computes the VAE loss function.
    #     """
    #     recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
    #     kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
    #     loss = torch.mean(recons_loss + kld_loss, dim=0)
        
    #     return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}


    def training_step(self, batch, batch_idx, optimizer_idx):
        data, target = batch
        target = target.to(torch.float)


        # train generator
        if optimizer_idx == 0:

            # generate images
            recons_x, x, real_label, fake_label = self(data, target)

            loss_dict = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
            self.log("Generator Traning Loss - GAN error", loss_dict["loss"])
            return loss_dict["loss"]

        # train discriminator
        if optimizer_idx == 1:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x, real_label, fake_label = self(data, target)
            loss_dict = self.adversarial_loss(real_label, torch.ones_like(real_label))
            self.log("Discriminator Traning Loss - Real",loss_dict["loss"])

            return loss_dict["loss"]
        
        # train discriminator
        if optimizer_idx == 2:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x, real_label, fake_label = self(data, target)
            loss_dict = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))

            self.log("Discriminator Traning Loss - Fake ",loss_dict["loss"])

            return loss_dict["loss"]


    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)

        recons_x, x, real_label, fake_label = self(data, target)

        loss_Discriminator_real = self.adversarial_loss(real_label, torch.ones_like(real_label))
        self.log("Discriminator loss validation real image", loss_Discriminator_real["loss"])
        loss_Discriminator_fake = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))
        self.log("Discriminator loss validation fake image", loss_Discriminator_fake["loss"])
        loss_Generator = self.adversarial_loss(fake_label, torch.ones_like(fake_label))
        self.log("GAN_loss validation fake image", loss_Generator["loss"])

        # loss_dict = self.loss_function(recons_x, x, mu, log_var)
        # self.log_dict({
        #     'val_loss': loss_dict['loss'],
        #     'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
        #     'val_KLD': loss_dict['KLD']
        # })

    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, real_label, fake_label = self(data, target)
        
        loss_Discriminator_real = self.adversarial_loss(real_label, torch.ones_like(real_label))
        self.log("Discriminator loss test real image", loss_Discriminator_real["loss"])
        loss_Discriminator_fake = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))
        self.log("Discriminator loss test fake image", loss_Discriminator_fake["loss"])
        loss_Generator = self.adversarial_loss(fake_label, torch.zeros_like(fake_label))
        self.log("GAN_loss test fake image", loss_Generator["loss"])

        # loss_dict = self.loss_function(recons_x, x, mu, log_var)
        # self.log_dict({
        #     'test_loss': loss_dict['loss'],
        #     'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
        #     'test_KLD': loss_dict['KLD']
        # })

    def configure_optimizers(self):
        optimizer1 = optim.AdamW(self.generator.parameters(), lr=self.lr)
        optimizer2 = optim.AdamW(self.discriminator.parameters(), lr = self.lr_discriminator)

        return [ optimizer1, optimizer2, optimizer2]#, [lr_scheduler_config_1,lr_scheduler_config_2]
        
################################################################################################################################

class ViTVAE_PatchGAN(LightningModule):
    def __init__(
        self,
        image_size=(128,128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf = 8,
        dropout=0.0,
        emb_dropout=0.0,
        landa = 100,
        lr=1e-4,
        lr_discriminator = 1e-4,
        frequency_generator = 1,
        frequency_discriminator = 1
        ):
        
        super().__init__()


        self.generator = Generator(image_size=image_size,
                                   patch_size=patch_size,
                                   num_classes=num_classes,
                                   dim=dim,
                                   depth=depth,
                                   heads=heads,
                                   mlp_dim=mlp_dim,
                                   channels=channels,
                                   dim_head=dim_head,
                                   ngf=ngf,
                                   dropout=dropout,
                                   emb_dropout=emb_dropout)
        

        # For now we will have a normal Discriminator; then I will change it to PatchGAN
        self.discriminator = Discriminator_Patch()
        self.landa =landa
        self.lr = lr
        self.lr_discriminator = lr_discriminator
        self.save_hyperparameters()
        self.freq_generator = frequency_generator
        self.freq_discriminator = frequency_discriminator
        self.dim = dim
        self.loss_object = nn.BCEWithLogitsLoss()
        self.loss_object_cross = nn.BCEWithLogitsLoss()

    def forward(self, img, labels):
        # # Generator
        out, mean, log_var = self.generator(img,labels)

        real_label = self.discriminator(img)
        fake_label = self.discriminator(out)

        return out, img, mean, log_var, real_label, fake_label
    
    
    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        samples = self.generator.sample(num_samples=num_samples,label=label)
        return samples

    def discriminator_loss(self, real_label, fake_label):
        # Loss with the real image
        real_loss = self.loss_object(real_label, torch.ones_like(real_label))
        # Loss with the generated image
        generated_loss = self.loss_object(fake_label, torch.zeros_like(fake_label))
        return {"loss_real":real_loss, "loss_fake":generated_loss}

    def generator_loss(self,fake_label, out, img):
        # Want to make the answer of the discriminator all close to one
        gan_loss = self.loss_object_cross(fake_label, torch.ones_like(fake_label))
        return {"loss": gan_loss}  

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}


    def training_step(self, batch, batch_idx, optimizer_idx):
        data, target = batch
        target = target.to(torch.float)

        # train generator
        if optimizer_idx == 0:

            # generate images
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.loss_function(recons_x, x, mu, log_var)
            self.log("Generator Traning Loss - VAE error", loss_dict["loss"])
            return loss_dict["loss"]

        # train generator
        if optimizer_idx == 1:

            # generate images
            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

            loss_dict = self.generator_loss(fake_label, recons_x, x)
            self.log("Generator Traning Loss - Generator error",loss_dict["loss"])
            return loss_dict["loss"]

        # train discriminator
        if optimizer_idx == 2:
            # Measure discriminator's ability to classify real from generated samples

            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator real_loss', loss_dict['loss_real'])

            return loss_dict["loss_real"]

        # train discriminator
        if optimizer_idx == 3:
            # Measure discriminator's ability to classify real from generated samples

            recons_x, x, mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator fake_loss', loss_dict['loss_fake'])

            return loss_dict["loss_fake"]


    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)

        recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("GAN_loss validation real image", loss_Discriminator["loss_real"])
        self.log("GAN_loss validation fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("GAN_loss validation fake image", loss_Generator["loss"])

        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })

    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("GAN_loss test real image", loss_Discriminator["loss_real"])
        self.log("GAN_loss test fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("GAN_loss test fake image", loss_Generator["loss"])

        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })
        
    
    def configure_optimizers(self):
        optimizer1 = optim.AdamW(self.generator.parameters(), lr=self.lr)
        optimizer2 = optim.AdamW(self.discriminator.parameters(), lr = self.lr_discriminator)
        lr_scheduler1 = optim.lr_scheduler.ReduceLROnPlateau(optimizer1, patience=6)
        lr_scheduler_config_1 = {
            "scheduler": lr_scheduler1,
            "interval": "epoch",
            "monitor": "val_loss",}
        lr_scheduler2 = optim.lr_scheduler.ReduceLROnPlateau(optimizer2, patience=6)
        lr_scheduler_config_2 = {
            "scheduler": lr_scheduler2,
            "interval": "epoch",
            "monitor": "val_loss",}
        return [optimizer1, optimizer1, optimizer2, optimizer2], [lr_scheduler_config_1,lr_scheduler_config_2]

   ############################################################################################################################


class ViTVAE_PatchGAN_prepared(LightningModule):
    def __init__(
        self,
        image_size=(128,128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf = 8,
        dropout=0.0,
        emb_dropout=0.0,
        landa = 100,
        lr=1e-4,
        lr_discriminator = 1e-4,
        frequency_generator = 1,
        frequency_discriminator = 1
        ):
        
        super().__init__()


        self.generator = CViTVAE().load_from_checkpoint("/work3/s164564/Vision-transformers-for-generative-modeling/models/CViTVAE2022-04-29-1735/CViTVAE-epoch=174.ckpt")
     
        

        # For now we will have a normal Discriminator; then I will change it to PatchGAN
        self.discriminator = Discriminator_Patch()
        self.landa =landa
        self.lr = lr
        self.lr_discriminator = lr_discriminator
        self.save_hyperparameters()
        self.freq_generator = frequency_generator
        self.freq_discriminator = frequency_discriminator
        self.dim = dim
        self.loss_object = nn.BCEWithLogitsLoss()

    
    def forward(self, img, labels):
        # # Generator
        
        out = self.generator.forward_2(img,img.shape[0])
        mean = 1
        log_var = 2
        real_label = self.discriminator(img)
        fake_label = self.discriminator(out)

        return out, img, mean, log_var, real_label, fake_label
    


    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        samples = self.generator.sample(num_samples=num_samples,label=label)
        return samples

    def discriminator_loss(self, real_label, fake_label):
        # Loss with the real image
        real_loss = self.loss_object(real_label, torch.ones_like(real_label))
        # Loss with the generated image
        generated_loss = self.loss_object(fake_label, torch.zeros_like(fake_label))
        return {"loss_real":real_loss, "loss_fake":generated_loss}

    def generator_loss(self,fake_label, out, img):
        # Want to make the answer of the discriminator all close to one
        gan_loss = self.loss_object( fake_label, torch.ones_like(fake_label))
        
        return {"loss":gan_loss}  


    def training_step(self, batch, batch_idx, optimizer_idx):
        data, target = batch
        target = target.to(torch.float)


        # train generator
        if optimizer_idx == 0:

            # generate images
            recons_x, x, mu, log_var,  real_label, fake_label = self(data, target)

            loss_dict = self.generator_loss(fake_label, recons_x, x)
            self.log("Generator Traning Loss - Generator error",loss_dict["loss"])
            return loss_dict["loss"]

        # train discriminator real
        if optimizer_idx == 1:
            # Measure discriminator's ability to classify real from generated samples

            recons_x, x,  mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator real_loss', loss_dict['loss_real'])

            return loss_dict["loss_real"]
        
        # train discriminator fake
        if optimizer_idx == 2:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x,  mu, log_var, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator fake_loss', loss_dict['loss_fake'])

            return loss_dict["loss_fake"]


    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)

        recons_x, x,  mu, log_var, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("Discriminator validation real image", loss_Discriminator["loss_real"])
        self.log("Discriminator validation fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("Generator validation image", loss_Generator["loss"])


    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x,  mu, log_var, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("Discriminator test real image", loss_Discriminator["loss_real"])
        self.log("Discriminator test fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("Generator test fake image", loss_Generator["loss"])


    def configure_optimizers(self):
        optimizer1 = optim.AdamW(self.generator.parameters(), lr=self.lr)
        optimizer2 = optim.AdamW(self.discriminator.parameters(), lr = self.lr_discriminator)
        # lr_scheduler1 = optim.lr_scheduler.ReduceLROnPlateau(optimizer1, patience=6)
        # lr_scheduler_config_1 = {
        #     "scheduler": lr_scheduler1,
        #     "interval": "epoch",
        #     "monitor": "val_loss",
        # }
        # lr_scheduler2 = optim.lr_scheduler.ReduceLROnPlateau(optimizer2, patience=6)
        # lr_scheduler_config_2 = {
        #     "scheduler": lr_scheduler2,
        #     "interval": "epoch",
        #     "monitor": "val_loss",
        # }
        return [optimizer1, optimizer2, optimizer2]#, [lr_scheduler_config_1, lr_scheduler_config_2]
    
    
    
    
    
    
    
    
class ViTVAE_PatchGAN_prepared_GEN(LightningModule):
    def __init__(
        self,
        image_size=(128,128),
        patch_size=16,
        num_classes = 6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf = 8,
        dropout=0.0,
        emb_dropout=0.0,
        landa = 100,
        lr=1e-4,
        lr_discriminator = 1e-4,
        frequency_generator = 1,
        frequency_discriminator = 1
        ):
        
        super().__init__()


        self.generator =  CViTVAE().load_state_dict(torch.load("/work3/s164564/Vision-transformers-for-generative-modeling/models/CViTVAE2022-04-08-2022/CViTVAE-epoch=191.ckpt"), strict=False)
        
     
        

        # For now we will have a normal Discriminator; then I will change it to PatchGAN
        self.discriminator = Discriminator_Patch()
        self.landa =landa
        self.lr = lr
        self.lr_discriminator = lr_discriminator
        self.save_hyperparameters()
        self.freq_generator = frequency_generator
        self.freq_discriminator = frequency_discriminator
        self.dim = dim
        self.loss_object = nn.BCEWithLogitsLoss()


    def forward(self, img, labels):
        num_samples = img.shape[0]
        # # Generator
        out = self.generator.forward_2(img,labels,num_samples)
        # # Discriminator
        real_label = self.discriminator(img)
        fake_label = self.discriminator(out)

        return out, img, real_label, fake_label
    


    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        samples = self.generator.decoder(z)
        return samples

    def discriminator_loss(self, real_label, fake_label):
        # Loss with the real image
        real_loss = self.loss_object(real_label, torch.ones_like(real_label))
        # Loss with the generated image
        generated_loss = self.loss_object(fake_label, torch.zeros_like(fake_label))
        return {"loss_real":real_loss, "loss_fake":generated_loss}

    def generator_loss(self,fake_label, out, img):
        # Want to make the answer of the discriminator all close to one
        gan_loss = self.loss_object( fake_label, torch.ones_like(fake_label))
        
        return {"loss":gan_loss}  


    def training_step(self, batch, batch_idx, optimizer_idx):
        data, target = batch
        target = target.to(torch.float)


        # train generator
        if optimizer_idx == 0:

            # generate images
            recons_x, x, real_label, fake_label = self(data, target)

            loss_dict = self.generator_loss(fake_label, recons_x, x)
            self.log("Generator Traning Loss - Generator error",loss_dict["loss"])
            return loss_dict["loss"]

        # train discriminator real
        if optimizer_idx == 1:
            # Measure discriminator's ability to classify real from generated samples

            recons_x, x, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator real_loss', loss_dict['loss_real'])

            return loss_dict["loss_real"]
        
        # train discriminator fake
        if optimizer_idx == 2:
            # Measure discriminator's ability to classify real from generated samples

            # how well can it label as real?
            recons_x, x, real_label, fake_label = self(data, target)
            loss_dict = self.discriminator_loss(real_label, fake_label)
            self.log('Discriminator fake_loss', loss_dict['loss_fake'])

            return loss_dict["loss_fake"]


    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)

        recons_x, x, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("Discriminator validation real image", loss_Discriminator["loss_real"])
        self.log("Discriminator validation fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("Generator validation image", loss_Generator["loss"])


    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, real_label, fake_label = self(data, target)

        loss_Discriminator = self.discriminator_loss(real_label, fake_label)
        self.log("Discriminator test real image", loss_Discriminator["loss_real"])
        self.log("Discriminator test fake image", loss_Discriminator["loss_fake"])
        loss_Generator = self.generator_loss(fake_label, recons_x, x)
        self.log("Generator test fake image", loss_Generator["loss"])



    def configure_optimizers(self):
        optimizer1 = optim.AdamW(self.generator.parameters(), lr=self.lr)
        optimizer2 = optim.AdamW(self.discriminator.parameters(), lr = self.lr_discriminator)
        lr_scheduler1 = optim.lr_scheduler.ReduceLROnPlateau(optimizer1, patience=6)
        lr_scheduler_config_1 = {
            "scheduler": lr_scheduler1,
            "interval": "epoch",
            "monitor": "val_loss",
        }
        lr_scheduler2 = optim.lr_scheduler.ReduceLROnPlateau(optimizer2, patience=6)
        lr_scheduler_config_2 = {
            "scheduler": lr_scheduler2,
            "interval": "epoch",
            "monitor": "val_loss",
        }
        return [optimizer1, optimizer2, optimizer2], [lr_scheduler_config_1, lr_scheduler_config_2]



class ViT(LightningModule):
    def __init__(
        self,
        *,
        image_size,
        patch_size,
        num_classes,
        dim,
        depth,
        heads,
        mlp_dim,
        pool="cls",
        channels=3,
        dim_head=64,
        dropout=0.0,
        emb_dropout=0.0,
        lr=3e-5,
    ):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert (
            image_height % patch_height == 0 and image_width % patch_width == 0
        ), "Image dimensions must be divisible by the patch size."

        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim = channels * patch_height * patch_width
        assert pool in {
            "cls",
            "mean",
        }, "pool type must be either cls (cls token) or mean (mean pooling)"

        self.to_patch_embedding = nn.Sequential(
            Rearrange(
                "b c (h p1) (w p2) -> b (h w) (p1 p2 c)",
                p1=patch_height,
                p2=patch_width,
            ),
            nn.Linear(patch_dim, dim),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)

        self.transformer = Transformer(dim, depth, heads, dim_head, mlp_dim, dropout)

        self.pool = pool
        self.to_latent = nn.Identity()

        self.mlp_head = nn.Sequential(nn.LayerNorm(dim), nn.Linear(dim, num_classes))
        self.lr = lr
        self.save_hyperparameters()
        self.optim_choice = optim_choice

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        cls_tokens = repeat(self.cls_token, "() n d -> b n d", b=b)
        x = torch.cat((cls_tokens, x), dim=1)
        x += self.pos_embedding[:, : (n + 1)]
        x = self.dropout(x)

        x = self.transformer(x)

        x = x.mean(dim=1) if self.pool == "mean" else x[:, 0]

        x = self.to_latent(x)
        return self.mlp_head(x)

    def loss(self, y_hat, y):
        return F.cross_entropy(y_hat, y)

    def training_step(self, batch, batch_idx):
        data, target = batch
        output = self(data)
        loss = self.loss(output, target)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        data, target = batch
        output = self(data)
        loss = self.loss(output, target)
        acc = (output.argmax(dim=1) == target).float().mean()
        self.log("val_acc", acc)
        self.log("val_loss", loss)
        return loss

    def test_step(self, batch, batch_idx):
        data, target = batch
        output = self(data)
        loss = self.loss(output, target)
        acc = (output.argmax(dim=1) == target).float().mean()
        self.log("test_acc", acc)
        self.log("test_loss", loss)
        return {"loss": loss, "acc": acc}

    def configure_optimizers(self):
        if self.optim_choice == "Adam":
            optimizer = optim.Adam(self.parameters(), lr=self.lr)
        if self.optim_choice == "SGD":
            optimizer = optim.SGD(self.parameters(), lr=self.lr, momentum=0.9)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=6)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "frequency": 1,
            "monitor": "val_loss",
            "strict": True,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}


class ViTVAE(LightningModule):
    def __init__(
        self,
        image_size=(128, 128),
        patch_size=16,
        dim=256,
        depth=12,
        heads=16,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf=64,
        dropout=0.0,
        emb_dropout=0.0,
        lr=5e-5,
    ):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert (
            image_height % patch_height == 0 and image_width % patch_width == 0
        ), "Image dimensions must be divisible by the patch size."

        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim = channels * patch_height * patch_width

        self.dim = dim
        self.mean_token = nn.Parameter(torch.randn(1, 1, dim))
        self.log_var_token = nn.Parameter(torch.randn(1, 1, dim))

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 2, dim))

        self.to_patch_embedding = nn.Sequential(
            Rearrange(
                "b c (h p1) (w p2) -> b (h w) (p1 p2 c)",
                p1=patch_height,
                p2=patch_width,
            ),
            nn.Linear(patch_dim, dim),
        )

        self.dropout = nn.Dropout(emb_dropout)

        self.encoder_transformer = Transformer(
            dim, depth, heads, dim_head, mlp_dim, dropout
        )

        self.decoder_conv = nn.Sequential(
            # input is Z, going into a convolution
            nn.ConvTranspose2d(dim, ngf * 16, (4, 4), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 16),
            nn.ReLU(True),
            # state size. (ngf*8) x 4 x 4
            nn.ConvTranspose2d(ngf * 16, ngf * 8, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 8),
            nn.ReLU(True),
            # state size. (ngf*4) x 8 x 8
            nn.ConvTranspose2d(ngf * 8, ngf * 4, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.ReLU(True),
            # state size. (ngf*2) x 16 x 16
            nn.ConvTranspose2d(ngf * 4, ngf * 2, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 2),
            nn.ReLU(True),
            # state size. (ngf) x 32 x 32
            nn.ConvTranspose2d(ngf * 2, ngf, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf),
            nn.ReLU(True),
            # state size. (ngf) x 64 x 64
            nn.ConvTranspose2d(ngf, channels, (4, 4), (2, 2), (1, 1), bias=False),
            nn.Tanh()
            # state size. (nc) x 128 x 128
        )

        self.lr = lr
        self.save_hyperparameters()

    def encoder(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        log_var_tokens = repeat(self.log_var_token, "() n d -> b n d", b=b)
        x = torch.cat((log_var_tokens, x), dim=1)

        mean_tokens = repeat(self.mean_token, "() n d -> b n d", b=b)
        x = torch.cat((mean_tokens, x), dim=1)

        x += self.pos_embedding

        x = self.dropout(x)

        x = self.encoder_transformer(x)

        return x

    def decoder(self, x):
        x = rearrange(x, "b d -> b d 1 1")

        x = self.dropout(x)

        x = self.decoder_conv(x)

        return x

    def reparameterize(self, mean, log_var):

        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)

        z = eps.mul(std).add_(mean)

        return z

    def forward(self, img):
        x = self.encoder(img)

        mean = x[:, 0]
        log_var = x[:, 1]
        z = self.reparameterize(mean, log_var)

        out = self.decoder(z)

        return img, out, mean, log_var

    def sample(self, num_samples):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """
        z = torch.randn(num_samples, self.dim)

        samples = self.decoder(z)

        return samples

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}

    def training_step(self, batch, batch_idx):
        data, target = batch
        recons_x, x, mu, log_var = self(data)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict(loss_dict)
        return loss_dict['loss']

    def validation_step(self, batch, batch_idx):
        data, target = batch
        recons_x, x, mu, log_var = self(data)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })
    
    def test_step(self, batch, batch_idx):
        data, target = batch
        recons_x, x, mu, log_var = self(data)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })

    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "frequency": 1,
            "monitor": "val_loss",
            "strict": False,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}



class ConvCVAE(LightningModule):
    def __init__(
        self,
        image_size=(128, 128),
        num_classes=6,
        dim=256,
        channels=3,
        ngf=8,
        lr=1e-4,
    ):
        super().__init__()
        self.image_height, self.image_width = pair(image_size)

        self.lr = lr
        self.save_hyperparameters()

        self.dim = dim
        self.mean = nn.Linear((ngf*16)*4*4, dim)
        self.log_var = nn.Linear((ngf*16)*4*4, dim)

        self.label_embedding = nn.Linear(num_classes, self.image_height*self.image_width)
        self.decoder_input = nn.Linear(dim + num_classes, dim)

        self.encoder_conv = nn.Sequential(
            nn.Conv2d(channels + 1, out_channels=ngf, kernel_size= 3, stride= 2, padding = 1),
            nn.BatchNorm2d(ngf),
            nn.LeakyReLU(),
            
            nn.Conv2d(ngf, out_channels=ngf*2, kernel_size= 3, stride= 2, padding = 1),
            nn.BatchNorm2d(ngf*2),
            nn.LeakyReLU(),
            
            nn.Conv2d(ngf*2, out_channels=ngf*4, kernel_size= 3, stride= 2, padding = 1),
            nn.BatchNorm2d(ngf*4),
            nn.LeakyReLU(),
            
            nn.Conv2d(ngf*4, out_channels=ngf*8, kernel_size= 3, stride= 2, padding = 1),
            nn.BatchNorm2d(ngf*8),
            nn.LeakyReLU(),

            nn.Conv2d(ngf*8, out_channels=ngf*16, kernel_size= 3, stride= 2, padding = 1),
            nn.BatchNorm2d(ngf*16),
            nn.LeakyReLU(),)

        self.decoder_conv = nn.Sequential(
            # input is Z, going into a convolution
            nn.ConvTranspose2d(dim, ngf * 16, (4, 4), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 16),
            nn.ReLU(True),
            # state size. (ngf*8) x 4 x 4
            nn.ConvTranspose2d(ngf * 16, ngf * 8, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 8),
            nn.ReLU(True),
            # state size. (ngf*4) x 8 x 8
            nn.ConvTranspose2d(ngf * 8, ngf * 4, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.ReLU(True),
            # state size. (ngf*2) x 16 x 16
            nn.ConvTranspose2d(ngf * 4, ngf * 2, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 2),
            nn.ReLU(True),
            # state size. (ngf) x 32 x 32
            nn.ConvTranspose2d(ngf * 2, ngf, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf),
            nn.ReLU(True),
            # state size. (ngf) x 64 x 64
            nn.ConvTranspose2d(ngf, channels, (4, 4), (2, 2), (1, 1), bias=False),
            nn.Tanh()
            # state size. (nc) x 128 x 128
        )

    def encoder(self, img):

        x = self.encoder_conv(img)

        return x

    def decoder(self, z):
        result = self.decoder_input(z)
        result = rearrange(result, "b d -> b d 1 1")
        result = self.decoder_conv(result)
        return result

    def reparameterize(self, mean, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)

        z = eps.mul(std).add_(mean)

        return z

    def forward(self, img, labels):
        label_embeddings = self.label_embedding(labels)
        label_embeddings = label_embeddings.view(-1,self.image_height,self.image_width).unsqueeze(1)
        x = torch.cat([img,label_embeddings], dim = 1)
        x = self.encoder(x)

        x = torch.flatten(x, start_dim=1)

        mean = self.mean(x)
        log_var = self.log_var(x)

        z = self.reparameterize(mean, log_var)
        z = torch.cat([z, labels], dim = 1)
        x = self.decoder(z)

        return x, img, mean, log_var
    
    def forward_2(self,img, num_samples):
        z = torch.randn(num_samples, self.dim, device=img.device)
        label = F.one_hot(torch.randint(0,5,(num_samples,),device=img.device),num_classes=6) #Generate random labels uniformly

        z = torch.cat([z, label], dim = 1)
        recons_img = self.decoder(z)

        return recons_img
    
    def encoding(self,img,labels):
        label_embeddings = self.label_embedding(labels)
        label_embeddings = label_embeddings.view(-1,self.image_height,self.image_width).unsqueeze(1)
        x = torch.cat([img,label_embeddings], dim = 1)
        x = self.encoder(x)

        x = torch.flatten(x, start_dim=1)

        mean = self.mean(x)
        log_var = self.log_var(x)

        z = self.reparameterize(mean, log_var)
        return z


    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        samples = self.decoder(z)
        return samples

    def sample_for_generation(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim, device=label.device)
        z = torch.cat([z, label], dim = 1)
        samples = self.decoder(z)
        return samples

    def sample_latent_space(self,num_samples,label):
        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)

        z = self.decoder_input(z)
        # samples = self.decoder(z)
        return z

    def reconstruct(self, img, label):
        reconstruction, img, _, _ = self(img,label)
        return reconstruction, img

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}

    def training_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict(loss_dict)
        return loss_dict['loss']

    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })
    
    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })


    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "frequency": 1,
            "monitor": "val_loss",
            "strict": False,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}

    

class CViTVAE_2(LightningModule):
    def __init__(
        self,
        image_size=(128, 128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf=8,
        dropout=0.0,
        emb_dropout=0.0,
        lr=1e-4,
    ):
        super().__init__()
        self.image_height, self.image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert (
            self.image_height % patch_height == 0 and self.image_width % patch_width == 0
        ), "Image dimensions must be divisible by the patch size."

        num_patches = (self.image_height // patch_height) * (self.image_width // patch_width)
        patch_dim = (1+channels) * patch_height * patch_width

        self.lr = lr
        self.save_hyperparameters()


        self.dim = dim
        self.mean_token = nn.Parameter(torch.randn(1, 1, dim))
        self.log_var_token = nn.Parameter(torch.randn(1, 1, dim))

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 2, dim))

        self.label_embedding = nn.Linear(num_classes, self.image_height*self.image_width)
        self.decoder_input = nn.Linear(dim + num_classes, dim)

        self.to_patch_embedding = nn.Sequential(
            Rearrange(
                "b c (h p1) (w p2) -> b (h w) (p1 p2 c)",
                p1=patch_height,
                p2=patch_width,
            ),
            nn.Linear(patch_dim, dim),
        )

        self.dropout = nn.Dropout(emb_dropout)

        self.encoder_transformer = Transformer(
            dim, depth, heads, dim_head, mlp_dim, dropout
        )

        self.decoder_conv = nn.Sequential(
            # input is Z, going into a convolution
            nn.ConvTranspose2d(dim, ngf * 16, (4, 4), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 16),
            nn.ReLU(True),
            # state size. (ngf*8) x 4 x 4
            nn.ConvTranspose2d(ngf * 16, ngf * 8, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 8),
            nn.ReLU(True),
            # state size. (ngf*4) x 8 x 8
            nn.ConvTranspose2d(ngf * 8, ngf * 4, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.ReLU(True),
            # state size. (ngf*2) x 16 x 16
            nn.ConvTranspose2d(ngf * 4, ngf * 2, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf * 2),
            nn.ReLU(True),
            # state size. (ngf) x 32 x 32
            nn.ConvTranspose2d(ngf * 2, ngf, (4, 4), (2, 2), (1, 1), bias=False),
            nn.BatchNorm2d(ngf),
            nn.ReLU(True),
            # state size. (ngf) x 64 x 64
            nn.ConvTranspose2d(ngf, channels, (4, 4), (2, 2), (1, 1), bias=False),
            nn.Tanh()
            # state size. (nc) x 128 x 128
        )

    def encoder(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        log_var_tokens = repeat(self.log_var_token, "() n d -> b n d", b=b)
        x = torch.cat((log_var_tokens, x), dim=1)

        mean_tokens = repeat(self.mean_token, "() n d -> b n d", b=b)
        x = torch.cat((mean_tokens, x), dim=1)

        x += self.pos_embedding

        x = self.dropout(x)

        x = self.encoder_transformer(x)

        return x

    def decoder(self, z):
        result = self.decoder_input(z)
        result = rearrange(result, "b d -> b d 1 1")
        result = self.decoder_conv(result)
        return result

    def reparameterize(self, mean, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)

        z = eps.mul(std).add_(mean)

        return z

    def forward(self, img, labels):
        label_embeddings = self.label_embedding(labels)
        label_embeddings = label_embeddings.view(-1,self.image_height,self.image_width).unsqueeze(1)
        x = torch.cat([img,label_embeddings],dim = 1)
        x = self.encoder(x)

        mean = x[:, 0]
        log_var = x[:, 1]

        z = self.reparameterize(mean, log_var)
        z = torch.cat([z, labels], dim = 1)
        x = self.decoder(z)

        return x, img, mean, log_var

    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        samples = self.decoder(z)
        return samples
    
    def reconstruct(self,img,label):
        reconstruction, img, _, _ = self(img,label)
        return reconstruction, img

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}

    def training_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict(loss_dict)
        return loss_dict['loss']

    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })
    
    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })


    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "frequency": 1,
            "monitor": "val_loss",
            "strict": False,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}


class CViTVAE(LightningModule):
    def __init__(
        self,
        image_size=(128, 128),
        patch_size=16,
        num_classes=6,
        dim=256,
        depth=4,
        heads=8,
        mlp_dim=256,
        channels=3,
        dim_head=64,
        ngf=8,
        dropout=0.0,
        emb_dropout=0.0,
        lr=1e-4
    ):
        super().__init__()

        self.generator = Generator(image_size=image_size,
                            patch_size=patch_size,
                            num_classes=num_classes,
                            dim=dim,
                            depth=depth,
                            heads=heads,
                            mlp_dim=mlp_dim,
                            channels=channels,
                            dim_head=dim_head,
                            ngf=ngf,
                            dropout=dropout,
                            emb_dropout=emb_dropout)



        self.lr = lr
        self.save_hyperparameters()
        self.dim = dim
        self.num_classes = num_classes




    def forward(self, img, labels):
        # # Generator
        out, mean, log_var = self.generator(img,labels)

        return out, img, mean, log_var
    
    def forward_2(self,img, num_samples):
        z = torch.randn(num_samples, self.dim, device=img.device)
        label = F.one_hot(torch.randint(0,self.num_classes-1,(num_samples,),device=img.device),num_classes=self.num_classes) #Generate random labels uniformly

        z = torch.cat([z, label], dim = 1)
        recons_img = self.generator.decoder(z)

        return recons_img

    def encoding(self,img,labels):
        return self.generator.encoding(img,labels)
    

    def sample(self, num_samples, label):
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)
        samples = self.generator.decoder(z)
        return samples

        def sample_for_generation(self, num_samples, label):
            """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :return: (Tensor)
        """

        z = torch.randn(num_samples, self.dim, device=label.device)
        z = torch.cat([z, label], dim = 1)
        samples = self.generator.decoder(z)
        return samples

    def sample_latent_space(self,num_samples,label):
        z = torch.randn(num_samples, self.dim)
        labels = repeat(label, "d -> n d",n=num_samples)
        z = torch.cat([z, labels], dim = 1)

        z = self.generator.decoder_input(z)
        # samples = self.generator.decoder(z)
        return z
    
    def reconstruct(self,img,label):
        reconstruction, img, _, _ = self(img,label)
        return reconstruction, img

    def loss_function(self,recons_x, x, mu, log_var):
        """
        Computes the VAE loss function.
        """
        recons_loss = torch.sum(F.mse_loss(recons_x.view(recons_x.shape[0],-1), x.view(x.shape[0],-1),reduction="none"),dim=1)
        
        kld_loss = -0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1)
    
        loss = torch.mean(recons_loss + kld_loss, dim=0)
        
        return {'loss': loss, 'Reconstruction_Loss':torch.mean(recons_loss.detach()), 'KLD':torch.mean(kld_loss.detach())}

    def training_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict(loss_dict)
        return loss_dict['loss']

    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'val_loss': loss_dict['loss'],
            'val_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'val_KLD': loss_dict['KLD']
        })
    
    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        recons_x, x, mu, log_var = self(data, target)
        loss_dict = self.loss_function(recons_x, x, mu, log_var)
        self.log_dict({
            'test_loss': loss_dict['loss'],
            'test_Reconstruction_Loss': loss_dict['Reconstruction_Loss'],
            'test_KLD': loss_dict['KLD']
        })


    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "monitor": "val_loss",
            "strict": False,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}



class Classifier(LightningModule):
    def __init__(self,
                 lr=1e-4):
        super().__init__()

        # init a pretrained resnet
        backbone = models.convnext_tiny(pretrained=True)
        layers = list(backbone.children())[:-1]
        self.feature_extractor = nn.Sequential(*layers)

        num_classes = 6
        self.classifier = nn.Sequential(
            models.convnext.LayerNorm2d((768,), eps=1e-06, elementwise_affine=True), nn.Flatten(1), nn.Linear(768, num_classes)
        )
        self.loss_function = nn.CrossEntropyLoss()
        self.lr = lr
        self.save_hyperparameters()

    def forward(self, x):
        self.feature_extractor.eval()
        with torch.no_grad():
            representations = self.feature_extractor(x)
        x = self.classifier(representations)
        return x.softmax(dim=1)
    
    def training_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        pred = self(data)
        loss = self.loss_function(pred,target)
        self.log("train_loss",loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Train Accuracy", acc)
        return loss

    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        pred = self(data)
        loss = self.loss_function(pred,target)
        self.log("val_loss", loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Validation Accuracy", acc)

    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        pred = self(data)
        loss = self.loss_function(pred,target)
        self.log("test_loss", loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Test Accuracy", acc)
    
    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        return self(batch.argmax(dim=1))

    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "monitor": "val_loss",
            "strict": False,
        }

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}

class Classifier_with_generation(LightningModule):
    def __init__(self,
                 lr:float = 1e-4,
                 generator:str = "ViTVAE"):
        super().__init__()

        if generator == "ViTVAE":
            self.generator = CViTVAE().load_from_checkpoint("/work3/s164564/Vision-transformers-for-generative-modeling/models/CViTVAE2022-04-29-1735/CViTVAE-epoch=174.ckpt")
            # self.generator = CViTVAE().load_from_checkpoint("F:\Vision-transformers-for-generative-modeling\models\CViTVAE2022-04-29-1735\CViTVAE-epoch=174.ckpt")
        elif generator == "ConvVAE":
            self.generator = ConvCVAE().load_from_checkpoint("/work3/s164564/Vision-transformers-for-generative-modeling/models/ConvCVAE2022-04-30-1854/ConvCVAE-epoch=349.ckpt")
            # self.generator = ConvCVAE().load_from_checkpoint("F:\Vision-transformers-for-generative-modeling\models\ConvCVAE2022-04-30-1854\ConvCVAE-epoch=349.ckpt")
        elif generator == "GAN":
            self.generator = None #Todo
            raise Exception("not implemented yet")
        else:
            raise Exception("generator name not recognized")
        self.generator.freeze()

        # init a pretrained resnet
        backbone = models.convnext_tiny(pretrained=True)
        layers = list(backbone.children())[:-1]
        self.feature_extractor = nn.Sequential(*layers)

        self.num_classes = 6
        self.classifier = nn.Sequential(
            models.convnext.LayerNorm2d((768,), eps=1e-06, elementwise_affine=True), nn.Flatten(1), nn.Linear(768, self.num_classes)
        )
        self.loss_function = nn.CrossEntropyLoss()
        self.lr = lr
        self.save_hyperparameters()

    def forward(self, x):
        # Generate images 
        self.feature_extractor.eval()
        with torch.no_grad():
            representations = self.feature_extractor(x)
        x = self.classifier(representations)
        return x.softmax(dim=1)

    def training_step(self, batch, batch_idx):
        img, target = batch
        target = target.to(torch.float)

        #Generate images such that mini-batch has uniform class distribution
        num_samples = target.shape[0]
        temp = F.relu(torch.ones(self.num_classes,device=target.device)*int(num_samples*2/self.num_classes) - torch.bincount(target.argmax(dim=1)), inplace=True)
        if torch.sum(temp) < num_samples:
            temp[temp.argmax()] += num_samples-torch.sum(temp)
        elif torch.sum(temp) > num_samples:
            nonzero_idx = temp.nonzero().flatten()
            diff = int((torch.sum(temp) - num_samples) / len(nonzero_idx))
            for i in nonzero_idx:
                temp[i] -= diff
            if torch.sum(temp) > num_samples:
                temp[temp.argmax()] -= torch.sum(temp) - num_samples
        temp0 = torch.ones(int(temp[0])) * 0
        temp1 = torch.ones(int(temp[1])) * 1
        temp2 = torch.ones(int(temp[2])) * 2
        temp3 = torch.ones(int(temp[3])) * 3
        temp4 = torch.ones(int(temp[4])) * 4
        temp5 = torch.ones(int(temp[5])) * 5
        target_gen = F.one_hot(torch.cat((temp0,temp1,temp2,temp3,temp4,temp5)).to(torch.int64),num_classes=6)
        target_gen = target_gen.to(target.device)

        img_gen = self.generator.sample_for_generation(num_samples=num_samples,label=target_gen)

        img = torch.cat([img,img_gen],dim=0)
        target = torch.cat([target,target_gen],dim=0)

        pred = self(img)
        loss = self.loss_function(pred,target)
        self.log("train_loss",loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Train Accuracy", acc)
        return loss

    def validation_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        pred = self(data)
        loss = self.loss_function(pred,target)
        self.log("val_loss", loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Validation Accuracy", acc)

    def test_step(self, batch, batch_idx):
        data, target = batch
        target = target.to(torch.float)
        pred = self(data)
        loss = self.loss_function(pred,target)
        self.log("test_loss", loss)
        acc = torch.sum(pred.argmax(dim=1) == target.argmax(dim=1))/len(target)
        self.log("Test Accuracy", acc)

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        return self(batch.argmax(dim=1))

    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.lr)
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5,factor=0.5)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",
            "monitor": "val_loss",
            "strict": False,
        }
        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}